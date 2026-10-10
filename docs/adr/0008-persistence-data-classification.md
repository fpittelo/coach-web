# MADR-0008: Persist Coaching Lifecycle Data in SQLite (WAL) with App-Level Field Encryption

- **Status:** Accepted (2026-10-10 — security conditions C1–C6 embedded; see Security review outcome)
- **Date:** 2026-10-10
- **Deciders:** @architect, @fpittelo (PO approval: epic #161 grooming decisions + Sprint 12 go-ahead, 2026-10-10); security routing: @cyber-security (review completed 2026-10-10 — conditional approval, conditions C1–C6 embedded below)

## Context

Epic #161 (End-to-End Coaching Lifecycle Management, v0.9) turns Coach Web from a reactive query console into a complete coaching loop: long-term athlete objectives → weekly microcycle planning → post-session debriefs → longitudinal review. This requires the first internal persistence in the repository — today Coach Web holds **no** durable state of its own (no database dependency in `pyproject.toml`; the only persistence is the GitHub plan branch and per-lane env files).

Five data classes are implicated (see classification table below). Two of them — debrief entries and weekly review summaries — are **health data** (RPE 1–10, sensation ratings, qualitative physical/mental-sensation notes). Under the Swiss nLPD (Fedlex SR 235.1, Art. 5(c): health data as particularly sensitive personal data), storage residency is fixed local-first by ADR-007 (dev/qa/prod run in loopback-bound Docker lanes on the workstation; the workstation itself is single-user, n=1). The retained cross-border residual is the OpenRouter LLM call, assessed in docs/security.md (#112) — this ADR does not change it.

Intervals.icu remains the SSOT for telemetry, workouts, and wellness records, accessed on demand through the `coach-mcp` sidecar (repo `fpittelo/coach`, #163).

This spike (#162) blocks Stories 1.1, 1.2 and 2.1 of epic #161. Implementation is out of scope — this record only fixes the decision.

## Decision Drivers

- **Swiss nLPD:** health data (debriefs, review summaries) must be protected at rest with encryption; collection must stay proportional/minimal (data-minimization over retention).
- **ADR-007 local-first topology:** single workstation Docker host, hardened containers (non-root, `read_only` fs, `cap_drop: ALL`, `no-new-privileges`), loopback-only binding; no new privileged container or host-side mount mechanism may be introduced.
- **n=1 single user:** no concurrent writers, no multi-tenant isolation requirements, no network-facing DB endpoint.
- **KIS (ADR-007 rationale):** zero operational burden is a core architectural value; the persistence engine must not add a service to babysit.
- **SSOT discipline:** telemetry/workouts/wellness already live in Intervals.icu; duplicating large time-series locally would create a second source of truth and a reconciliation burden.
- **Backup story:** "lose the machine" must not mean losing coaching history; backups must not leak health data even unencrypted at the backup target.
- **Secrets discipline (#112):** per-lane, per-service env files (`chmod 600`, gitignored); no shared `.env`; secrets never logged or in the repository.
- **Existing dependency tree:** `pyjwt[crypto]` already resolves `cryptography` transitively (pyproject.toml:25) — app-level crypto is nearly free to introduce. *(Security condition C6: `cryptography` must be declared as a direct dependency with a version floor at implementation — transitive resolution via pyjwt is fragile.)*

## Considered Options

### Engine

#### Option E1: SQLite in WAL mode (chosen)

- Pros: zero operational burden (in-process library, no extra container); n=1 means single-writer — WAL mode makes reads concurrent with a writer and readers never block; single file = trivially backupable via the online backup API (`sqlite3 .backup` / `VACUUM INTO`) with no dump tooling; built-in JSON1 extension covers the structured-rating and snapshot payloads; standard `sqlite3` CLI available for inspection; SQLite is famously well-tested ([SQLite how-it-works](https://www.sqlite.org/howitworks.html)).
- Cons: single-file at-rest exposure (the DB file *is* the data — mitigated below); SQLite `ALTER TABLE` limitations require batch-mode migrations; no network access to the DB (irrelevant: one container, loopback lane); single-writer at a time (irrelevant at n=1).

#### Option E2: PostgreSQL

- Pros: real concurrent writers, richer type system, mature ecosystem, trivial `pg_dump`.
- Cons: needs a second container per lane (×3 lanes), memory overhead, backup/retention policy, upgrade cadence — a service to operate, violating the KIS driver; its benefits (concurrency, network) address problems that do not exist at n=1; the DB would still sit inside the same trust boundary (same host, same lane network), adding attack surface without adding isolation.

#### Option E3: Intervals.icu-only (no internal persistence)

- Pros: zero new state; no new attack surface; no migration story needed.
- Cons: objectives, debrief entries, plan approval states, and weekly reviews have no fit in the Intervals.icu data model (it stores fitness data, not coach-conversation artifacts, drafts, or approval workflow state); Coach Web would lose offline operation and query capability for its own loop; weekly plan drafts/approval states would have to be smuggled into GitHub or Intervals payloads — untyped and fragile; would push *more* health data onto the Intervals.icu platform, the opposite of the local-residency direction of ADR-007.

### Encryption at rest for health data

**Threat model fit (what are we protecting against):** (1) disk theft / machine loss; (2) backup leakage — a copy of the volume or backup archive leaving the workstation boundary; (3) container-volume compromise — another container or an attacker with filesystem access reading the mounted volume. We are *not* protecting against the coach-web process itself (it must decrypt to function), against a fully compromised app process, or against the workstation user.

#### Option X1: SQLCipher (whole-database encryption)

- Pros: transparent at the storage layer — the entire file is AES-256 encrypted; no schema discipline needed ("everything sensitive is encrypted"); `PRAGMA key` from env is a small integration.
- Cons: whole-file encryption also blocks standard tooling (`sqlite3` CLI, `.backup` to plaintext, data browsers) unless the tooling is sqlcipher-aware; requires a sqlcipher build in the image (C library + Python binding, e.g. `pysqlcipher3`/`sqlcipher3`) — heavier image builds, a dependency not in the current tree; the community edition carries AGPL licensing considerations (Zetetic commercial license otherwise); encrypts the low-sensitivity tables (objectives, plan drafts) for no security gain, at the cost of losing queryability on the *plaintext-safe* data.

#### Option X2: Filesystem / volume-level encryption (LUKS, host FDE)

- Pros: protects everything on the volume with zero application code; workstation FDE is already part of the nLPD posture (ADR-007 consequences, #112/#113).
- Cons: in the Docker-on-host topology, the volume lives on the host filesystem — the app **cannot enforce or verify** that the host volume is LUKS-encrypted (a LUKS block device inside the container would need privileged mounts, violating the hardened-container posture, `cap_drop: ALL`); any copy taken *out* of the FDE boundary (a backup archive moved elsewhere, a volume snapshot) is plaintext and unprotected; protection is invisible to the application and cannot be asserted in code or tested by the zero-warning gate. As a *sole* mechanism, protection extends only as far as the FDE boundary — exactly the boundary backups cross.

#### Option X3: App-level field encryption — `cryptography` Fernet on sensitive columns (chosen)

- Pros: the sensitive fields (debrief entries, review summaries) are ciphertext wherever the file travels — inside the volume, in a backup, on a copied archive; plaintext data (objectives, plan drafts, Intervals snapshots already public to Intervals.icu) stays queryable and inspectable; `cryptography` is already in the dependency tree via `pyjwt[crypto]` — no new dependency; Fernet is an authenticated construction (AES-128-CBC + HMAC-SHA256) — tampering is detected, not just concealed ([Fernet spec](https://cryptography.io/en/latest/fernet/)); field granularity matches the data classification: protect what is classified, not everything.
- Cons: application code must encrypt/decrypt (one small helper module); encrypted fields cannot be SQL-queried (acceptable at n=1 — debrief fields are never queried, only fetched by id/date range on unencrypted columns); column-level granularity leaks coarse metadata (dates, table shapes) — assessed as low sensitivity; the application becomes the decryption oracle by definition.

**Key lifecycle — `DATA_ENCRYPTION_KEY` (dedicated) vs derived from `AUTH_SESSION_SECRET`:**

A dedicated, per-lane `DATA_ENCRYPTION_KEY` env-file secret (32-byte urlsafe-base64 Fernet key, `chmod 600`, gitignored, per-lane distinct — same discipline as `AUTH_SESSION_SECRET`, #112) is chosen. Deriving the data key from `AUTH_SESSION_SECRET` was considered and rejected:

1. **Rotation coupling:** the session secret is an *ephemeral authentication* secret, rotated cheaply on suspicion of compromise. If the data key is derived from it, every session-secret rotation bricks the persisted health data (or forces a re-encryption migration keyed off an auth event). Data-at-rest keys and auth keys must have independent lifecycles.
2. **Blast radius / compartmentalization:** `AUTH_SESSION_SECRET` signs HS256 session tokens and lives in the transmitted authentication path. Compromise of the auth secret should not automatically expose the entire health-data archive; a derived key makes one compromise compound into both.
3. **Lane semantics differ:** dev/qa run `AUTH_ENABLED=false` (ADR-007) yet still hold debrief data in the lane volume; a data key tied to an auth secret is semantically wrong for lanes that authenticate nobody.

Key-loss consequence: an unrecoverable `DATA_ENCRYPTION_KEY` means unrecoverable encrypted fields (plaintext tables survive). Mitigation in the backup/restore story below.

**Key lifecycle (security conditions C2/C4/C5, from the 2026-10-10 @cyber-security review):**

- **Escrow mechanism (C2):** the per-lane `DATA_ENCRYPTION_KEY` is escrowed in a password manager **plus** one offline copy on a separate encrypted medium; the key backup is **never co-located** with the DB backup. Key recovery is an explicit step in the restore runbook and is exercised in every restore rehearsal.
- **Rotation (C4):** rotation uses `cryptography.fernet.MultiFernet` with a key list (or an equivalent `key_version` column) so rotation is online, idempotent, and reversible — a crash mid-rotation must never leave mixed ciphertext that cannot be attributed to a key. The rotation runbook (add new key as first MultiFernet entry → re-encrypt pass → drop old key) is specified at implementation.
- **In-process handling (C5):** the key is loaded as `pydantic.SecretStr` (via pydantic-settings), never logged, never `repr`'d; a redaction regression test pins this. Known residual: env-var injection means the key is visible to `docker inspect` and `/proc/<pid>/environ` on the host — accepted under the threat model below.

**Threat-model boundary (explicit, per security review):** Fernet + dedicated key addresses disk theft (LUKS primary, Fernet defense-in-depth), backup leakage (for encrypted classes), and container-volume compromise (ciphertext on the volume). It provides **no protection against full host compromise**: the key (env file) and the ciphertext (lane volume) are co-located on the same host filesystem, both under the FDE boundary. This exclusion is accepted for the single-user local-first topology; the boundary that actually matters is the **backup boundary**, where key and DB backups must never be co-located.

### Migration tooling

#### Option M1: Alembic + SQLAlchemy (chosen)

- Pros: the repository has no migration tooling today (nothing to replace); Alembic gives versioned up/down migrations, autogenerate from models, and a documented answer to SQLite's `ALTER TABLE` limitations (`render_as_batch=True` batch mode, [Alembic batch operations](https://alembic.sqlalchemy.org/en/latest/batch.html)).
- Cons: introduces SQLAlchemy + Alembic as new dependencies (greenfield persistence needs a data layer anyway); autogenerate must be reviewed — never trusted blindly.

#### Option M2: raw versioned SQL scripts

- Pros: zero dependencies; full control over the SQLite dialect.
- Cons: no down-migrations, no autogenerate, no state tracking beyond a homemade `schema_version` table; re-implementing what Alembic provides, poorly.

### Backup mechanism

#### Option B1: nightly online `sqlite3 .backup` / `VACUUM INTO` (chosen)

- Pros: WAL-safe online backup (never copies a torn file; [SQLite backup API](https://www.sqlite.org/backup.html)) — a host-side cron can snapshot the volume file without stopping the lane; with health fields app-encrypted, the C2/C4 fields are ciphertext-safe in backups (C1/C3 remain plaintext — see the C1 qualification in Backup/restore); restore is "stop lane → replace file → start lane".
- Cons: full-copy each night (size is trivial at this data volume); backup dir placement is a host decision; the encryption key must be recoverable separately from the backup or backups of encrypted fields are unreadable.

#### Option B2: Litestream streaming replication

- Pros: near-continuous WAL shipping, point-in-time restore.
- Cons: another service per lane; designed for durability against server loss — overkill for a workstation lane; deferred (reconsider only if the lane grows a multi-host story).

## Decision Outcome

Chosen option: **E1 + X3 + M1 + B1** — *SQLite (WAL) as the internal store; sensitive health fields encrypted at the application layer with Fernet under a dedicated per-lane `DATA_ENCRYPTION_KEY`; Alembic for schema migrations; nightly online backup with ciphertext-safe archives* — because it is the only combination that satisfies the KIS driver (no new service), keeps health data ciphertext at every boundary the data crosses (volume, backup, any off-host copy), respects the hardened-container posture (no privileged mounts), and reuses the existing `cryptography` dependency tree. PostgreSQL (E2) solves problems that do not exist at n=1; Intervals.icu-only (E3) cannot represent the coaching loop's own state; SQLCipher (X1) buys whole-file encryption at the cost of tooling, licensing, and image complexity for no added protection over field encryption given the threat model; LUKS-only (X2) protects nothing that leaves the FDE boundary and cannot be enforced from inside a hardened container.

### Data classification table

| # | Data class | Examples | Store | Protection at rest | Retention | Rationale |
|:--|:---|:---|:---|:---|:---|:---|
| C1 | Athlete objectives & targets | goals, availability notes, FTP/watts targets, milestone dates | SQLite (WAL), table `athlete_objectives` + `periodization_phases` | plaintext | until achieved/abandoned, then archived rows retained 1 season, then purged | tactical coaching state; derived personal data, low sensitivity; needed unencrypted for plan generation queries |
| C2 | Debrief entries | RPE 1–10, sensation ratings, qualitative notes | SQLite (WAL), table `debrief_entries` | **Fernet field encryption** (all debrief body fields: `rpe`, `sensation_ratings`, `qualitative_notes_enc`) | indefinite (longitudinal athlete memory) — purge on explicit owner request, **plus a configurable hard cap (default 5 years) with a documented purge path** | **Health data, nLPD Art. 5(c)**; ciphertext in volume and backups; never queried (fetch by unencrypted date/id only). Storage-limitation rationale (nLPD Art. 5(c) proportionality): the longitudinal memory is the data's *purpose* — the athlete's own coaching history — with owner-purge + cap as the limitation control. Known residual (security review F9): plaintext `session_date` + `intervals_workout_id` leak debrief existence/timing metadata; RPE encryption is classification consistency, not meaningful protection against metadata analysis |
| C3 | Weekly plan drafts & approval states | plan markdown, draft/approved/rejected status, approval timestamps | SQLite (WAL), table `plan_drafts` | plaintext | active drafts kept; superseded drafts pruned after 4 weeks | operational workflow state; plan text already exists in the GitHub plan branch (same sensitivity) |
| C4 | Weekly review snapshots | point-in-time CTL/ATL/TSB snapshot, review summary, adherence score | SQLite (WAL), table `weekly_reviews` | snapshot payload plaintext (already resides in Intervals.icu); **summary fields Fernet-encrypted** (`summary_enc`) | rolling 52 weeks, older rows purged | snapshots duplicate Intervals SSOT data deliberately (point-in-time, offline availability); review narrative is personal/health context → encrypted. Classification rationale (security review F7): the snapshot duplicates data Intervals.icu already holds under the same athlete identity — the local copy adds *availability*, not *exposure*; the newly-created narrative is the sensitive part and is encrypted |
| C5 | Telemetry, workouts, wellness records | HR/power/pace streams, wellness scores, activity details | **Intervals.icu (SSOT)** — on-demand fetch via `coach-mcp`, never persisted by Coach Web | n/a (platform-managed) | platform-managed | avoids duplicating large time-series and a second SSOT; the Intervals.icu transfer is already assessed in docs/security.md (#112) |

No `user_id` column anywhere: the application is single-tenant by construction (whitelist admits exactly one Google account, docs/security.md) — a per-row user dimension would be dead weight. This is revisited only if multi-athlete support ever enters scope.

### Schema sketch (field level)

All timestamps are UTC ISO-8601 TEXT (SQLite convention); JSON payloads use the built-in JSON1 type affinity (TEXT). Fernet-encrypted columns are suffixed `_enc` (urlsafe-base64 token strings). FKs enforce `ON DELETE CASCADE` where a child is meaningless without its parent.

**`athlete_objectives`**

| Column | Type | Constraints / notes |
|:---|:---|:---|
| `id` | INTEGER | PK AUTOINCREMENT |
| `objective_type` | TEXT | CHECK in (`outcome`, `process`, `milestone`) |
| `title` | TEXT | NOT NULL |
| `description` | TEXT | nullable |
| `target_metric` | TEXT | nullable, e.g. `ftp`, `20min_watts`, `event_time` |
| `target_value` | REAL | nullable |
| `target_date` | TEXT (date) | nullable |
| `availability_notes` | TEXT | nullable (training-days-per-week, constraints) |
| `status` | TEXT | CHECK in (`active`, `achieved`, `abandoned`), default `active` |
| `created_at` / `updated_at` | TEXT | NOT NULL |

**`periodization_phases`**

| Column | Type | Constraints / notes |
|:---|:---|:---|
| `id` | INTEGER | PK AUTOINCREMENT |
| `objective_id` | INTEGER | FK → `athlete_objectives.id`, CASCADE |
| `phase_type` | TEXT | CHECK in (`base`, `build`, `peak`, `taper`, `recovery`, `competition`) |
| `name` | TEXT | NOT NULL |
| `start_date` / `end_date` | TEXT (date) | NOT NULL; CHECK `start_date` ≤ `end_date` |
| `focus` | TEXT | nullable |
| `weekly_hours_target` | REAL | nullable |
| `notes` | TEXT | nullable |
| `created_at` / `updated_at` | TEXT | NOT NULL |

**`debrief_entries`** — health data, encrypted body fields

| Column | Type | Constraints / notes |
|:---|:---|:---|
| `id` | INTEGER | PK AUTOINCREMENT |
| `session_date` | TEXT (date) | NOT NULL, indexed (queryable, plaintext) |
| `intervals_workout_id` | INTEGER | nullable, indexed — link to Intervals.icu activity when one exists |
| `plan_draft_id` | INTEGER | nullable FK → `plan_drafts.id` (debrief on a planned session) |
| `rpe` | TEXT (`_enc`) | **Fernet-encrypted**; plaintext form INTEGER CHECK 1–10, validated in Pydantic before encryption |
| `sensation_ratings` | TEXT (`_enc`) | **Fernet-encrypted**; plaintext form JSON object (per-dimension 1–10 ratings) |
| `qualitative_notes_enc` | TEXT (`_enc`) | **Fernet-encrypted** free-text notes |
| `source` | TEXT | CHECK in (`chat`, `manual`) |
| `created_at` | TEXT | NOT NULL |

**`plan_drafts`**

| Column | Type | Constraints / notes |
|:---|:---|:---|
| `id` | INTEGER | PK AUTOINCREMENT |
| `week_start_date` / `week_end_date` | TEXT (date) | NOT NULL; CHECK `week_start_date` ≤ `week_end_date` |
| `content` | TEXT | NOT NULL (plan markdown) |
| `status` | TEXT | CHECK in (`draft`, `submitted`, `approved`, `rejected`, `superseded`), default `draft` |
| `approved_at` | TEXT | nullable |
| `github_issue_ref` | TEXT | nullable (plan-issue link in `fpittelo/coach`) |
| `created_at` / `updated_at` | TEXT | NOT NULL |

**`weekly_reviews`**

| Column | Type | Constraints / notes |
|:---|:---|:---|
| `id` | INTEGER | PK AUTOINCREMENT |
| `week_start_date` | TEXT (date) | NOT NULL, unique, indexed |
| `snapshot` | TEXT (JSON) | point-in-time metrics fetched from Intervals.icu (CTL/ATL/TSB, volume) — plaintext, C4 |
| `summary_enc` | TEXT (`_enc`) | **Fernet-encrypted** review narrative |
| `adherence_score` | REAL | nullable (planned vs completed sessions) |
| `debrief_count` | INTEGER | NOT NULL default 0 |
| `created_at` | TEXT | NOT NULL |

**`debrief_dismissal_flags`** — supports the pending-debrief banner (#164) without nagging

| Column | Type | Constraints / notes |
|:---|:---|:---|
| `id` | INTEGER | PK AUTOINCREMENT |
| `session_date` | TEXT (date) | NOT NULL — the dismissed debrief target |
| `dismissed_at` | TEXT | NOT NULL |
| `reason` | TEXT | nullable — **bounded to a fixed enum** (`done`, `not-today`, `injury`) per security review F10; free-text health context must not land here |
| `expires_at` | TEXT | nullable — flag auto-ignorable after this point (banner may re-surface) |

Indexes: `debrief_entries(session_date)`, `debrief_entries(intervals_workout_id)`, `plan_drafts(week_start_date)`, `periodization_phases(objective_id)`, `weekly_reviews(week_start_date)` (unique).

### Migration strategy

- SQLAlchemy 2.0 models + Alembic (`render_as_batch=True` for the SQLite `ALTER TABLE` workaround); migrations are plain files under `alembic/versions/`, reviewed in PRs like any other code.
- Dev lane: migrations run automatically at container start (entrypoint step). Prod lane: migrations run as an explicit entrypoint step with the lane stopped/idle (n=1, sub-second execution — no zero-downtime requirement); a failed migration aborts startup rather than half-applying.
- Initial migration (0001) creates only `athlete_objectives` (amended 2026-10-10, PR #171 review: story #166 KIS scoping — the objective profile is the first persisted data class); the remaining tables arrive with their own stories as incremental migrations; no backfill (no existing data to migrate).
- Down-migrations are authored but untested for data-destructive steps (documented per migration).

### Backup / restore

- **Backup:** host-side cron (outside the container — the container stays `read_only`-hardened and unprivileged) runs a nightly `sqlite3 .backup` (online, WAL-safe) of the lane volume file into a versioned backup directory. **Backup-claim qualification (security condition C1):** only the C2/C4 encrypted fields are ciphertext-safe; **C1 (objectives, availability notes, FTP/watts targets) and C3 (plan drafts) travel in the archive as plaintext** — the archive is therefore NOT safe on an unencrypted target. The backup medium itself must be encrypted (e.g. an encrypted disk image or `age`/`gpg`-encrypted archive), or the claim is limited to "C2/C4 protected, C1/C3 exposed".
- **Key recovery:** `DATA_ENCRYPTION_KEY` must be recoverable independently of the machine or the encrypted data is lost — escrow per the key-lifecycle section (password manager + offline encrypted copy, never co-located with the DB backup). Key recovery is exercised in every restore rehearsal (security condition C2).
- **Restore:** stop lane → replace volume file with the backup → start lane → verify (health-check + a spot check of a recent debrief decryption). Restore is rehearsed before the first prod release of the persistence story; re-rehearsed quarterly.
- **Purge path:** C1/C3/C4 retention pruning and C2 owner-request purge are documented maintenance scripts, not silent background jobs.

## Consequences

- **Positive:** the coaching loop gains durable, offline-capable state with zero new services; health data is ciphertext in every location it exists outside the running process; C2/C4 fields are ciphertext-safe in backups; C1/C3 require an encrypted backup medium (C1); the dependency cost is SQLAlchemy + Alembic + reusing the already-present `cryptography` package.
- **Negative:** app code gains an encryption helper and an encryption-key dependency (a lost key destroys C2/C4 summary data); encrypted columns are unqueryable; SQLAlchemy/Alembic are new dependency surface and new review targets; SQLite enforces single-writer discipline that would need re-architecture if the n=1 assumption ever falls; column-level encryption leaves table metadata (dates, status) in plaintext.
- **Mitigations:** key stored and backed up per the backup/restore section; all debrief queries routed by unencrypted `session_date`/`id`; dependency additions reviewed under the zero-warning CI gate; n=1 assumption recorded in this ADR and revisited on any scope change; retention pruning scripts make data-minimization explicit and auditable.

### Security routing (MADR-0008 — completed 2026-10-10)

`@cyber-security` reviewed this draft against arc42 §8 and §11 plus `docs/security.md` (B1–B10) on 2026-10-10. **Verdict: conditional approval — the decision (E1 + X3 + M1 + B1) is sound and proportionate; acceptance conditioned on amendments C1–C6, all of which are now embedded in this document:**

- **C1** — backup claim qualified: C1/C3 travel plaintext in the archive; encrypted backup medium required (backup/restore section).
- **C2** — key-escrow mechanism specified (password manager + offline copy, never co-located with DB backup); key recovery exercised in restore rehearsals (key lifecycle section).
- **C3** — `chmod 600` enforcement for the `DATA_ENCRYPTION_KEY` env file + a pre-flight permission assertion required at implementation. **Repo finding (F3): the live lane env files are currently mode 644 — remediation tracked as a follow-up issue; the persistence implementation must enforce 600 for the new key file.**
- **C4** — rotation via `MultiFernet`/key-versioning, online and reversible (key lifecycle section).
- **C5** — key loaded as `SecretStr`, never logged, redaction regression test required (key lifecycle section).
- **C6** — `cryptography` declared as a direct dependency with a version floor; C4 snapshot classification rationale and C2 storage-limitation rationale + configurable cap recorded (classification table).

Companion verdict (spike #163): the Intervals.icu least-privilege scope set `ACTIVITY:READ, CALENDAR:WRITE, WELLNESS:READ, CHATS:WRITE` is **approved as the target**; for MVP the personal API key is acceptable for the single-user loopback topology **iff** write tools are confirmation-gated, reads are `oldest`-bounded (≤90 days/call), write operations are audit-logged, comment content is server-generated from validated data, and the greeting check is cached/budgeted. These conditions are binding on the Epic-3/microcycle story specs and the `coach-mcp` tool specs (tracked in #163).

On acceptance: index in §11 of docs/architecture.md and update arc42 §3/§5 for the new persistence component (follow-up docs task).

### Open questions — resolved 2026-10-10

1. **Key escrow:** resolved by C2 — password manager + offline encrypted copy, never co-located with the DB backup; recovery rehearsed.
2. **C2 retention:** resolved by C6 — indefinite owner-purgeable **plus** configurable hard cap (default 5 years) with documented purge path.
3. **`rpe` encryption depth:** resolved by F9 — keep RPE encrypted for classification consistency (cheap at n=1); the plaintext-metadata leak is documented in the C2 row and accepted.
4. **MADR sequence gap:** convention question deferred — 0001–0007 remain sections of `docs/architecture.md`; the madr-adr lint reconciliation is tracked as a docs follow-up, not a security item.
5. **Backup target placement:** resolved by C1 — the medium must be encrypted; exact host path is a PO implementation-time decision recorded in the persistence story.

### References

- SQLite WAL: <https://www.sqlite.org/wal.html> — online backup API: <https://www.sqlite.org/backup.html>
- Fernet symmetric encryption (authenticated AES-128-CBC + HMAC-SHA256): <https://cryptography.io/en/latest/fernet/>
- SQLCipher (Zetetic; community edition AGPL / commercial licensing): <https://www.sqlcipher.net/>
- Alembic batch operations for SQLite: <https://alembic.sqlalchemy.org/en/latest/batch.html>
- Swiss nLPD, Art. 5(c) particularly sensitive personal data: <https://www.fedlex.admin.ch/eli/cc/2020/648/en>
- Repo-internal: ADR-007 (docs/architecture.md §10), docs/security.md (secret boundaries #112, cross-border assessment #112), epic #161 grooming positions, issue #162.
