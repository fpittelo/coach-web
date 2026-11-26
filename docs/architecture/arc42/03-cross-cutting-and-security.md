# 3. Cross-cutting Concerns, Quality & Decisions — Coach Web

**Status:** active (consolidated #369)

---

## 1. Cross-cutting Concepts

### 1.1 Security & Privacy (Swiss nLPD / FADP)

Full threat model and STRIDE boundary tables live in [`docs/security.md`](../../security.md). Core cross-cutting controls:

- **Data Classification & Cryptographic Envelopes ([MADR-0008](../../adr/0008-persistence-data-classification.md), [MADR-0025](../../adr/0025-conversation-memory-model-retention-and-injection-contract.md), [MADR-0032](../../adr/0032-durable-longitudinal-memory-snapshot-store.md)):**
  - **C1 (Operational / Structural Metadata):** dates, sport types, workout step structures, enums (`autonomy_mode`, `verbosity`, `environment_preference`), and logistical `standing_preferences` (≤1000 chars) — stored in typed SQLite columns with CHECK constraints.
  - **C2 (Sensitive Biometric / Qualitative Health & Conversation Data):** conversation message bodies (`content_encrypted`), rolling thread summaries (`summary_encrypted`), and qualitative debrief notes (`notes_encrypted`) — encrypted at the application layer with `Fernet(DATA_ENCRYPTION_KEY)` before hitting SQLite, and sealed again inside a **whole-file Fernet envelope** (`VACUUM INTO` inside a `0700`/`0600` private scratch directory) before upload to `gs://coach-web-509209-snapshots` (`EUROPE-WEST6`).
  - **Bounded Retention & Synchronous Erase:** raw messages older than `90` days and inactive threads older than `365` days are swept by `maintenance.purge_expired()` with immediate snapshot republication; explicit athlete purge (`POST /api/conversation/purge`) awaits synchronous GCS snapshot overwrite before returning `200` ([MADR-0032](../../adr/0032-durable-longitudinal-memory-snapshot-store.md) C5).
- **Defense-in-Depth Authentication & Least-Privilege IAM ([MADR-0027](../../adr/0027-cloud-run-production-operating-model.md), [MADR-0031](../../adr/0031-public-landing-surface-with-iap-unchanged.md)):**
  - Console edge requires **both** Cloud Run direct-on-service IAP (`roles/run.invoker` == `{IAP service agent}`) **and** in-app Google OIDC session validation (`AUTH_WHITELIST_EMAILS`).
  - Public landing runs as a separate Cloud Run service (`coach-web-landing`) on a zero-role service account with zero secrets, zero cookies, and no console routes.
  - Every deploy executes fail-closed IAM, secret-wiring (D4), bucket-hardening (C1/C4/C5), and live-probe assertions before declaring success.
- **Prompt-Injection & XSS Containment (ADR-006, [MADR-0025](../../adr/0025-conversation-memory-model-retention-and-injection-contract.md) Amendment 0002, [MADR-0026](../../adr/0026-admin-orientation-and-agent-settings-prompt-precedence-and-sanitizer-contract.md), [MADR-0032](../../adr/0032-durable-longitudinal-memory-snapshot-store.md) C7/C8):**
  - Untrusted text passes multi-site `sanitize_untrusted_text` (stripping control chars, role tags, and instruction-override patterns) at every injection boundary: owner orientation, standing preferences, the intra-session `session_metrics_block` (per-domain sanitize + `"| "` `splitlines()` fence), conversation memory (read-for-summary and injection) and the cross-thread brief. Each block renders inside `| `-prefixed non-authoritative data fences and is always followed by `REASSERT_CLAUSE` structurally **LAST** in `assemble_system_prompt` — the clause names every such block as data/style guidance only, never a permission or tool-gating source.
  - Browser DOM binds user/settings/suggestion strings exclusively via Alpine `x-text` / `x-model`; `x-html` is restricted to assistant Markdown passed through vendored `DOMPurify.sanitize(...)`.
  - **Direct single-event calendar writes (#466, [MADR-0034](../../adr/0034-direct-conversational-calendar-writes.md)):** the model toolset is `COACH_MCP_ALLOWED_TOOLS` (19 reads + exactly 3 event writes). Before `hub.execute_tool`, deterministic guards enforce the C1 mutation budget (`7`/run), the C2 same-run verified `event_id` (update/delete only), the C3 `athlete_id="0"` self pin, and the C4 `description`/`workout_doc` preservation guard; the C5 hub gate resolves only bounded local `#/$defs` references before injecting defaults.
  - Errors and degraded persistence/context paths log **exception class names only** — never plaintext, ciphertext, tokens, or user input. Intra-session ledger reuse/invalidation logs are metadata-only (`reused_domain`, `invalidated_by_sync_signal`); event-write logs are metadata-only too (tool name, structurally-safe `event_id`, status, duration/counts — never `name`, `description`, `workout_doc` or tool-result bodies). The system-level audit is **two-tier**: the `coach-mcp` sidecar still records redacted full arguments on stderr (residual R8, routed to `fpittelo/coach#127`).

### 1.2 Quality Gates & CI

Every pull request targeting `main` and every push on `main` must pass with **0 warnings and 0 failures**:

- **Static & Type Analysis:** `ruff check .`, `black --check .`, `isort --check-only .`, `mypy --strict src/coach_web tests`.
- **Automated Test & Executable Gate Suite:** `pytest -W error --cov=src/coach_web --cov-fail-under=80` (including `/bin/bash` + `gcloud`/`curl`-shim drift-rejection simulations for the Cloud Run deploy gates in `tests/integration/test_compose_topology.py`).
- **Containerized Pre-Push Harness:** `bash harness/run.sh python`.
- **Supply-Chain & Secret Scanning:** `gitleaks` (full git history), `pip-audit --strict`, `trivy` (`HIGH,CRITICAL`, `exit-code: 1` on both `Dockerfile` and `Dockerfile.landing`), `Compose Lane Validation` (`dev`/`qa`/`prod` + `check_sidecar_drift.sh`), and async trunk `deep-validation` (`mmdc` Mermaid rendering + relative-link check + secret scan, MADR-0028).

### 1.3 Configuration & Secrets

- **Pydantic v2 `Settings` (`src/coach_web/config.py`):** typed environment configuration with safe local defaults (`TRUSTED_HOSTS=127.0.0.1,::1`, `COACH_SNAPSHOT_BUCKET=""`).
- **Strict Secret Scoping:** `coach-web` holds 4 secrets (`OPENROUTER_API_KEY`, `GOOGLE_OIDC_CLIENT_SECRET`, `AUTH_SESSION_SECRET`, `DATA_ENCRYPTION_KEY`); `coach-mcp` holds `INTERVALS_API_KEY`; `github-mcp` holds `GITHUB_TOKEN`; `coach-web-landing` holds **zero** secrets. Enforced per container in Compose overrides and by the D4 `secretKeyRef` drift guard in `cloud-run-deploy.yaml`.
- **Key Escrow:** `DATA_ENCRYPTION_KEY` is escrowed offline by `@fpittelo` (never stored in GCS and never readable by `coach-web-deployer@`).

### 1.4 Language & Communication Policy

Per `home-governance` §2, all source code, tests, ADRs, `arc42` views, runbooks, and GitHub issues are authored in **English**; non-software personal topics use **French**.

### 1.5 Naming & Structure Conventions

- Feature/chore/fix branches follow `<type>/<issue-#>-<slug>` off `main` and squash-merge into `main` (MADR-0017).
- Database schema changes ship strictly as reversible batch-mode Alembic revisions under `src/coach_web/alembic/versions/NNNN_<slug>.py` tested for upgrade and downgrade data preservation.
- MCP tool names exposed to the agent are namespaced `<server>__<tool>` and filtered through `COACH_MCP_ALLOWED_TOOLS` (19 reads + the 3 scoped single-event writes, [MADR-0034](../../adr/0034-direct-conversational-calendar-writes.md)); every other upstream write is refused pre-hub.

---

## 2. Quality Requirements

| Scenario | Metric | Target |
| :--- | :--- | :--- |
| **Cold-start persistence integrity** | Corrupt ciphertext, tampered Fernet HMAC, missing key, or failed staged migration on Cloud Run boot | `100%` fail-closed (`exit 1`, `publish_enabled=false`, remote snapshot untouched) |
| **Erase propagation** | `POST /api/conversation/purge` durability | Returns `200` only after encrypted GCS snapshot overwrite succeeds (`503` otherwise) |
| **Zero-trust console isolation** | Unauthenticated `GET /` or `/api/*` to `coach-web` | `100%` intercepted at Google IAP edge (`302` / `401`, zero container reach) |
| **Public landing isolation** | Unauthenticated `GET /` vs `/auth/*`, `/api/*`, `/health*` on `coach-web-landing` | `200` on `/` and `/static/landing.css` (< 50 ms warm); `404` on all console paths; `0` cookies |
| **Test & type hygiene** | Statement coverage, pytest warnings, mypy strict errors, HIGH/CRITICAL CVEs | `≥ 80%` floor (`~98%` actual), `0` warnings, `0` mypy errors, `0` HIGH/CRITICAL CVEs |

---

## 3. Risks & Technical Debt

| Risk / Technical Debt | Impact | Mitigation / Posture | Tracking |
| :--- | :--- | :--- | :--- |
| **Intra-session MCP read re-fetching & date-window jitter** | Follow-up turns within the same chat thread re-query baseline Intervals.icu wellness/fitness tools (`~25s` latency) and can drift on `oldest` date parameters. | **Delivered, Epic #432:** ephemeral in-memory `(athlete_id, thread_id)` `SessionMetricsLedger` (MADR-0008 C5 compliant, zero disk/GCS writes) injected as `session_metrics_block` at slot 5, with a deterministic `today`-anchored baseline short-circuit (zero MCP calls / zero ICU budget on Turn 2+) and deterministic sync-cue invalidation. Residual `R5`: an athlete-synced workout mentioned without a sync cue is invisible for at most the 30-min ledger TTL (MADR-0025 Am. 0002). | Epic `#432` (`#433`–`#435`) — MADR-0025 Am. 0002 |
| **Direct conversational calendar writes — residual risk register (MADR-0034 R1–R8)** | The 3 single-event writes are authorized by the authenticated chat turn itself; indirect prompt injection inside the owner's data plane, LLM target misread, retry duplication and post-mutation plan drift remain possible (no in-app undo) although bounded by C1–C4. | PO-accepted at `n=1` (2026-11-26): C1 budget 7/run, C2 same-run verified `event_id`, C3 self `athlete_id`, C4 preservation guard, `approved_week_digest` live-calendar precedence note. R1–R8 recorded verbatim. | [MADR-0034](../../adr/0034-direct-conversational-calendar-writes.md) |
| **Two-tier audit & `coach-mcp` unauthenticated intra-pod endpoint** | `coach-web` logs metadata only, but the `coach-mcp` stderr audit records redacted full arguments (training prose, R8), and its MCP endpoint carries no bearer token inside the Cloud Run instance (R5/R6: audit `caller="anonymous"`, any pod-local process can bypass the allow-list). | Pre-existing, raised in value by #466. Routed: **F1** (#467 — `COACH_MCP_AUTH_TOKEN` + D4 6→7 secrets) and **F2** (`fpittelo/coach#127` — audit argument minimization). No system-wide "zero prose logged" claim is made. | #467 · `fpittelo/coach#127` |
| **Legacy `github-mcp` 3rd sidecar & `GITHUB_TOKEN` surface** | Adds 512 MiB pod memory, an upstream container pin, a runtime PAT, and connection warnings when `github-mcp` is unavailable, even though SQLite + GCS snapshots + Intervals.icu now cover durability and scheduling. | Retire `github-mcp`, dead `github_client.py`, and runtime `GITHUB_TOKEN` to reach a 2-container pod (`coach-web` + `coach-mcp`). | Epic `#440` (`#441`–`#443`, `coach#126`) |
| **Split-brain coaching methodology across CLI & Web** | Core HOME Rule #6 4-phase wattage and Kettlebell balance rules live in CLI `@coach` prompts rather than `DEFAULT_SYSTEM_PROMPT`, while personal habits need a quick Settings preset. | Embed immutable methodology in `DEFAULT_SYSTEM_PROMPT`, add a client-side starter-habit preset in Settings, and consolidate CLI onto `skills/coach/SKILL.md`. | Epic `#437` (`#438`–`#439`, `opencode-home-config#494`) |
| **Single-writer snapshot ceiling (`maxScale=1`, MADR-0032 R1/R2)** | Raising `maxScale > 1` would cause `412` generation conflicts; an abrupt `SIGKILL` before the 60 s debounce flushes can lose at most the last ≤60 s of chat writes. | Keep `maxScale=1` pinned for single-athlete (`n=1`) scope; `SIGTERM` flushes synchronously; revisit Cloud SQL / Litestream only if `maxScale > 1` is ever needed. | [MADR-0032](../../adr/0032-durable-longitudinal-memory-snapshot-store.md) §Consequences |
| **Single `DATA_ENCRYPTION_KEY` blast radius (MADR-0032 R3)** | Loss of `DATA_ENCRYPTION_KEY` without offline owner escrow renders encrypted SQLite columns and GCS snapshots permanently unrecoverable. | Mandatory offline owner escrow documented in [`docs/admin_guide.md`](../../admin_guide.md); Cloud KMS CMEK deferred as YAGNI unless multi-admin compliance requires it. | [MADR-0032](../../adr/0032-durable-longitudinal-memory-snapshot-store.md) C3 |
| **Legacy `README.md` top-level overview drift** | Root `README.md` still reflects early local-only Phase 1 phrasing prior to Cloud Run (`MADR-0027`), durable snapshots (`MADR-0032`), and `arc42` (`#369`). | Refresh root `README.md` against these `arc42` views once the active topology epics settle. | Grooming candidate |

---

## 4. Glossary

| Term | Definition |
| :--- | :--- |
| **`assemble_system_prompt`** | Single prompt-construction seam in `src/coach_web/agent.py` enforcing deterministic block order and placing `REASSERT_CLAUSE` structurally last. |
| **C1 / C2 Data Tiers** | [MADR-0008](../../adr/0008-persistence-data-classification.md) sensitivity tiers: **C1** = non-sensitive operational/structural metadata; **C2** = sensitive biometric, qualitative health, and conversation text requiring Fernet encryption at rest. |
| **`coach-mcp`** | Companion Python FastMCP sidecar (`fpittelo/coach`) wrapping the Intervals.icu REST API over SSE. |
| **CTL / ATL / TSB** | Banister impulse-response training load metrics from Intervals.icu: Chronic Training Load (fitness), Acute Training Load (fatigue), and Training Stress Balance (form). |
| **IAP** | Google Cloud Identity-Aware Proxy enforced directly on the `coach-web` Cloud Run service before traffic reaches any container. |
| **Snapshot Coordinator** | Lifecycle component in `src/coach_web/snapshot.py` managing 60 s debounced, `SIGTERM`-flushed, and purge-synchronous whole-file Fernet backups to GCS. |

---

## 5. Architecture Decisions (MADR)

**The single authoritative MADR index for this repository is [`docs/adr/README.md`](../../adr/README.md)** (portfolio-wide decisions live in `opencode-home-config/docs/adr/README.md`). Decision tables are never duplicated here.

### Pre-MADR Historical Decision Back-Index (`ADR-001` – `ADR-007`)

Decisions `ADR-001`, `ADR-002`, `ADR-006`, and `ADR-007` predated standalone files under `docs/adr/` (numbers `0003`–`0005` were never assigned; `MADR-0009` lives as the appendix of [`docs/e2e-checklist.md`](../../e2e-checklist.md)):

| Historical ID | Date | Status | Normative Summary & Current Disposition |
| :--- | :--- | :--- | :--- |
| **ADR-001 — Separate Repository for Coach Web** | 2026-09-05 | **Accepted** (active) | Splits `fpittelo/coach-web` (web cockpit) from `fpittelo/coach` (`coach-mcp` FastMCP server) along the MCP protocol boundary, isolating `INTERVALS_API_KEY` inside `coach-mcp` with independent CI/CD and GHCR lifecycles. |
| **ADR-002 — Streamlit Framework Choice** | 2026-09-05 | **Superseded** | Initial v0.1–v0.4 rapid prototype stack; completely replaced in v0.5+ by the async FastAPI + SSE + Alpine.js architecture (ADR-006). Zero Streamlit code remains in the repository. |
| **ADR-006 — v0.7 Conversational Visual & Rendering Contract** | 2026-09-19 | **Accepted** (amended by [MADR-0011](../../adr/0011-quick-reply-suggestions.md) & [MADR-0025](../../adr/0025-conversation-memory-model-retention-and-injection-contract.md)) | Establishes slate/teal v0.7 CSS design tokens, self-hosted Inter WOFF2, zero-CDN Alpine.js UI, `DOMPurify.sanitize(marked.parse(...))` restricted to assistant bubbles (`x-text` for all user/suggestion text), and `POST /api/agent/stream` (`{message, thread_id?}`) streaming typed SSE frames with server-owned `suggestions` on `done`. |
| **ADR-007 — Local-First Docker Compose Lanes** | 2026-10-03 | **Accepted** (amended by [MADR-0024](../../adr/0024-always-on-prod-lane-release-verified-auto-update.md) & [MADR-0027](../../adr/0027-cloud-run-production-operating-model.md)) | Defines the concurrent loopback-only workstation Compose lanes (`dev :8100`, `qa :8200`, `prod :8000`) with per-service secret scoping and hardened containers (`UID 10001`, `read_only`, `cap_drop: ALL`, `no-new-privileges`). Complemented by `systemd` release auto-update locally ([MADR-0024](../../adr/0024-always-on-prod-lane-release-verified-auto-update.md)) and the reinstated Cloud Run production lane ([MADR-0027](../../adr/0027-cloud-run-production-operating-model.md)). |
