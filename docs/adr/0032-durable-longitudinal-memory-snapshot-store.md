# MADR-0032: Durable Longitudinal Memory — GCS-Snapshot-Backed Local Store + Cross-Thread Brief + Standing Preferences

- **Status:** proposed
- **Date:** 2026-11-22
- **Deciders:** @architect, @fpittelo (PO ratification pending), @cyber-security (verification pending — MANDATORY before finalization per MADR-0008: new storage location for C2-class health data)

## Context

The athlete's coaching relationship has no continuity in the Cloud Run production lane. #415's
root-cause evidence (comment 7077460932, cited throughout) established the two gaps precisely:

1. **Store ephemerality (gap 2 — dominant, verified live):** prod mounts `/data` as an `emptyDir`
   with `medium: Memory` (tmpfs, MADR-0027 D3 / MADR-0021). Cloud Run reclaims idle instances, so
   the store is wiped on every scale-to-zero churn: **5 instance terminations in 4 h 26 m**
   (05:40Z–09:25Z window), each erasing threads, debriefs, and `agent_settings`. The 06:46
   "zero memory" observation is fully explained by this alone: the instance cold-started 2 m 28 s
   before the turn into an empty volume, `build_turn_memory_block` returned `None`, and the store
   itself died 30 minutes later — unrecoverable by design.
2. **Thread-scoped memory (gap 1 — independent, latent):** the MADR-0025 memory block
   (`build_memory_block`) is scoped to `thread_id`. Even with a durable store, every new thread
   inherits nothing ("new chat = blank").

**Both gaps must close.** Fixing only durability leaves new-chat amnesia; fixing only the brief
leaves every idle reclaim erasing history. Additional flags from #415: the current posture fails
**silently** (no signal distinguishes "empty by design" from "wiped"), instance reclamation is
mid-session-loss-capable, and the blast radius covers debriefs and agent settings, not just chat.

Constraints: Swiss nLPD posture (C2 health data — residency `europe-west6`, encryption at rest,
retention bounded); single-athlete corpus (tiny — YAGNI rules out vector RAG); KIS (smallest
diff satisfying the ACs); the app is async-first FastAPI over SQLAlchemy 2.0 + `aiosqlite`
(single 76-line `db.py` seam, WAL mode).

## Decision Drivers

- **Durability:** threads, messages, debriefs, and settings survive instance reclamation, deploys, and cold starts.
- **Swiss residency:** all durable copies live in `europe-west6` (Zürich); no data leaves Switzerland.
- **C6 encryption posture (MADR-0008 §C6):** app-level Fernet field encryption on C2 content columns is unchanged by this decision (defense in depth regardless of storage layer).
- **Zero silent data loss:** persistence state must be observable (degrade telemetry), per the #415 fail-open flag.
- **KIS/YAGNI:** no new database engine, no VPC plumbing, no new operational service class unless the ACs demand it.
- **Retention:** the MADR-0025 C6 purge windows (90/365-day) continue to bound everything durable.

## Considered Options

### Option 1 (chosen): GCS-snapshot-backed local store (rehydration + checkpointed snapshots)

Keep the existing SQLite/aiosqlite engine **byte-for-byte as-is** (local POSIX tmpfs is a fully
supported SQLite platform). Add a small persistence module that:

- **Restores at boot:** if `COACH_DB_PATH` is empty/absent and a snapshot object exists in the
  configured GCS bucket → download it (atomic single-object copy), verify integrity, then run the
  existing `alembic upgrade head`; emit a persistence-init event.
- **Snapshots on a bounded schedule:** after writes, a debounced checkpoint (e.g. 60 s) copies the
  database **atomically via SQLite's own backup path** (`VACUUM INTO` a side file / the sqlite3
  backup API — never a raw file copy under WAL) to `gs://<bucket>/<snapshot>.db` in a
  `europe-west6` bucket with CMEK; snapshot failure ⇒ `log_persistence_degrade` (the existing
  telemetry seam), never silent.
- **Bounded loss window:** the snapshot cadence bounds worst-case loss to the debounce period;
  with single-instance concurrency (`maxInstances=1`, MADR-0027) the snapshot path has no
  concurrent writer.

- Pros:
  - **Smallest diff that satisfies durability**: no new engine, no driver migration, no VPC
    connector, no connection-pool management in a serverless runtime; the 76-line `db.py` is untouched.
  - **No POSIX-semantics gamble**: SQLite runs on a real local FS; the bucket is touched only by
    whole-object copies — sidestepping the locking defect that disqualifies Option 2.
  - Residency trivially satisfied (regional bucket, CMEK); runtime SA gains only
    `storage.objectUser` **scoped to the dedicated bucket** (a C6-adjacent grant — explicitly put
    before @cyber-security).
  - The loss window is an honest, bounded, documented trade-off — proportionate for a single-user
    coaching journal with a 60 s debounce.
- Cons:
  - **Bounded data loss on SIGKILL** (instance reclaim mid-debounce): up to one debounce window of
    the latest writes. Mitigation: short debounce + snapshot also on graceful shutdown (SIGTERM path).
  - A crash between `VACUUM INTO` completion and object upload loses that snapshot attempt (retry next tick — acceptable).
  - Boot-time restore adds a few seconds of latency on cold start (single small file).

### Option 2 (rejected): SQLite on a Cloud Run GCS volume mount (gcsfuse in-place)

Mount a bucket as `/data` via Cloud Run's volume mounts (gen2, gcsfuse CSI) and keep the app unchanged.

- Cons (disqualifying — grounded):
  - Google's Cloud Run volume-mount documentation states: **"Cloud Storage FUSE does not provide
    concurrency control for multiple writes (file locking) to the same file. When multiple writes
    try to replace a file, the last write wins and all previous writes are lost"**, and the FUSE FS
    is **"not a fully POSIX-compliant file system"**
    (https://docs.cloud.google.com/run/docs/configuring/services/cloud-storage-volume-mounts).
    SQLite's WAL mode depends on POSIX locking; running it on gcsfuse risks silent corruption —
    the exact failure class this epic exists to eliminate.
  - gcsfuse memory tax (stat cache, ~64 MiB per streaming-write file) against the service's
    memory budget, for zero benefit over Option 1.

### Option 3 (rejected): Cloud SQL for PostgreSQL (`europe-west6`, CMEK)

Migrate the store to managed Postgres; swap the engine URL/driver (asyncpg) in the small seam.

- Pros: real managed durability, automated backups, CMEK, `europe-west6` is a supported region
  (https://docs.cloud.google.com/sql/docs/postgres/locations — Enterprise edition shared/dedicated core both available).
- Cons (disqualifying under KIS/YAGNI for now):
  - A **new operational service class** (instance cost, maintenance, private-IP/VPC plumbing or
    public-IP auth surface) for a single-athlete journal.
  - **Residency wrinkle:** Cloud SQL backups stored in the `eu` **multi-region** are documented as
    *not* stored in `europe-west6` or London
    (https://docs.cloud.google.com/sql/docs/postgres/locations) — Swiss-only residency requires
    pinning backups to a regional location, an extra configuration to get right (and audit).
  - Driver/dialect migration (aiosqlite→asyncpg, alembic dialect) is a moderate diff with its own
    regression surface — disproportionate while Option 1 satisfies the ACs.
  - Revisit trigger: if multi-device sync, concurrent writers, or corpus growth beyond the
    single-bucket scale ever materialize.

### Option 4 (rejected): `minInstances=1` alone (keep tmpfs, keep the instance warm)

- Pros: eliminates the **dominant** wipe trigger (scale-to-zero churn) with a config-only change.
- Cons (insufficient as the decision): deploys still wipe the store (new revision = new volume);
  it converts the service to always-billed (cost posture change vs MADR-0027's scale-to-zero); and
  it does nothing for gap 1. Evaluated as a **possible operational complement** to Option 1 (noted
  in Consequences), not as the durability decision.

### Option 5 (rejected): Firestore / Datastore

Paradigm migration (document-store semantics, different retention/encryption model) for a corpus
that is a handful of MB. YAGNI.

## Decision Outcome

Chosen option: **"Option 1 — GCS-snapshot-backed local store"**, because it is the smallest change
that makes the store durable across the verified wipe triggers (churn **and** deploys), keeps the
entire SQLite/SQLAlchemy/WAL stack untouched, satisfies Swiss residency with a regional CMEK
bucket, bounds worst-case loss to a documented debounce window, and converts the silent fail-open
flag into visible persistence telemetry. Options 2's locking defect is documented by Google as a
disqualifier; Option 3 is a disproportionate operational step for a single-user corpus (revisit
trigger recorded); Option 4 is a partial complement at best; Option 5 is YAGNI.

**This ADR also fixes the two model gaps** (gap 1 and preferences), since they share the memory
seam and were root-caused together:

- **Cross-thread brief (closes gap 1):** extend the existing MADR-0025 seam — a bounded,
  injection-fenced **cross-thread brief block** assembled in `conversation_context` alongside (not
  replacing) the current thread's window+summary. Sources: rolling summaries of the last N threads
  (N small, bounded), debrief key facts (existing debrief store), open action items. Same fencing
  ("treat as data, never instructions"), same sanitizer clamp discipline, same block-agnostic
  re-assert predicate as MADR-0025 C2. Fail-safe: empty history ⇒ byte-identical no-memory prompt.
- **Standing preferences (closes the preferences story):** extend the `agent_settings` store
  (MADR-0026) with the ratified free-form preference fields, injected via the existing
  orientation/settings path with fixed precedence (core → orientation → preferences → digests →
  memory → re-assert LAST), editable in the settings modal, retention-bounded like all store content.

## Consequences

- **Positive:** coaching continuity survives reclaims and deploys; new threads inherit recent
  history (debrief facts, plan adjustments, open actions); standing preferences persist without
  re-stating; persistence state is observable (no silent wipe); local VIDAR lane gains the same
  snapshot capability or keeps its durable disk (no regression either way).
- **Negative:** a new GCS bucket + a runtime-SA grant (`storage.objectUser`, bucket-scoped) enter
  the security surface; worst-case loss bounded by the debounce window (SIGKILL mid-debounce);
  boot sequence gains a restore step; two new prompt-content sources (brief, preferences) must
  respect the MADR-0025 injection-fencing contract.
- **Mitigations:** dedicated bucket in `europe-west6` with CMEK, no public access, versioning on
  (accidental-overwrite recovery); grant resource-scoped to that bucket only — **explicit
  verification items for @cyber-security**; snapshot integrity check at restore (SQLite integrity
  pragma) with fail-closed abort on a corrupt snapshot (keep the empty store + loud degrade rather
  than restore garbage); retention purge runs against the durable content per MADR-0025 C6 so the
  bucket snapshot never outlives the retention windows; the brief block is bounded (hard caps) and
  fenced (C2 contract); deploy gates updated in the lane workflow (bucket + SA binding assertions,
  the #396 C4 pattern); **implementation sequencing**: #417 (durability) lands before #418/#419
  (brief, preferences) — gap 1's content is worthless until it survives.
