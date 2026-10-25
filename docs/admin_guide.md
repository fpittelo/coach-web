# 📘 Admin Guide — Coach Web

**Audience:** @devops, @architect, @fpittelo  
**Last updated:** 2026-10-25

> **Deployment model: ADR-007 — Local-First** (`docs/architecture.md` §10). The local Docker host on the workstation is the deployment target for the dev, qa, and prod lanes. The GCP Cloud Run topology is retired (Sprint 08, #64–#68) and is decommissioned via #111.

---

## 🗺️ Deployment Model (ADR-007)

| Lane | Compose files | coach-web image | Host port | Auth | Purpose |
|:---|:---|:---|:---|:---|:---|
| **dev** | `compose.yaml` + `compose.dev.yml` | build from source | `127.0.0.1:8100` | `AUTH_ENABLED=false` | active development |
| **qa** | `compose.yaml` + `compose.qa.yml` | `ghcr.io/fpittelo/coach-web:qa` | `127.0.0.1:8200` | `AUTH_ENABLED=false` | staging validation |
| **prod** | `compose.yaml` + `compose.prod.yml` | digest-pinned `vX.Y.Z` | `127.0.0.1:8000` | `AUTH_ENABLED=true` (OIDC) | daily use |

- **All host bindings are `127.0.0.1` only** — loopback binding is the primary access control (ADR-007). Never bind `0.0.0.0`.
- Each lane runs as its own compose project (`coach-web-dev|qa|prod`, pinned via the lane file's `name:` field and the `-p` flag) with its own network (`<project>_coach-net`) — lanes can run concurrently without port/name collisions.
- Sidecars (`coach-mcp`, `github-mcp`) publish no host ports; they are reachable only inside the lane network.
- **Always pass `--env-file .env.<lane>`** — the lane env file is the interpolation source for the scoped `${...}` references in `compose.yaml`. Without it, compose falls back to a legacy `.env` in the repo root (or empty defaults), and containers start with missing secrets.

---

## 📦 Environment Variables

`.env.example` is the variable catalogue for the non-Docker dev flow (`uv run coach-web` reads `.env` directly). The Docker lanes use per-lane templates instead: `.env.dev.example`, `.env.qa.example`, `.env.prod.example`. Summary:

| Group | Variables | Notes |
|:---|:---|:---|
| **App** | `APP_HOST`, `APP_PORT`, `CORS_ORIGINS`, `LOG_LEVEL`, `CACHE_TTL_SECONDS`, `AGENT_MAX_TOOL_ITERATIONS` | uvicorn binding & app behaviour |
| **Auth (OIDC)** | `AUTH_ENABLED`, `GOOGLE_OIDC_CLIENT_ID`, `GOOGLE_OIDC_CLIENT_SECRET`, `GOOGLE_OIDC_ISSUER`, `AUTH_WHITELIST_EMAILS`, `AUTH_SESSION_SECRET`, `AUTH_SESSION_TTL_SECONDS`, `AUTH_REDIRECT_URI` | opt-in; fail-closed boot when enabled but misconfigured |
| **MCP sidecars** | `COACH_MCP_URL`, `GITHUB_MCP_URL`, `COACH_MCP_IMAGE`, `GITHUB_MCP_IMAGE` | compose service discovery (`http://coach-mcp:8000/sse`, `http://github-mcp:8001/`) |
| **GitHub** | `GITHUB_TOKEN`, `GITHUB_REPO`, `GITHUB_PLAN_BRANCH`, `GITHUB_PLAN_DIR`, `GITHUB_TOOLSETS` | PAT scope: `repo:read` (dev/qa) / read+write to plan branch (prod) |
| **OpenRouter** | `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, `OPENROUTER_MODEL`, `OPENROUTER_TIMEOUT_SECONDS` | coach agent LLM |
| **coach-mcp** | `INTERVALS_API_KEY`, `INTERVALS_ATHLETE_ID`, `INTERVALS_BASE_URL`, `MCP_TRANSPORT`, `MCP_HOST`, `MCP_PORT` | sidecar-local |

### Per-Lane Env Files (ADR-007, #109)

- `.env.dev`, `.env.qa`, `.env.prod` — one per lane, created from the matching `*.example` template, **never a shared `.env`**; each is `chmod 600` and gitignored.
- **Scoping mechanism:** the lane env file is passed to compose via `--env-file` and used for `${...}` **interpolation only** — no `env_file:` injects a whole file into any container. Each service's `environment:` map in `compose.yaml` names exactly the variables it needs, so a leaked lane file can do no more than the declared scope allows.
- **Per-service secret scoping:** `coach-web` receives only `OPENROUTER_API_KEY`, `GOOGLE_OIDC_CLIENT_SECRET`, `AUTH_SESSION_SECRET`, `GITHUB_TOKEN` (+ app config); `coach-mcp` only `INTERVALS_API_KEY` (+ MCP config); `github-mcp` only `GITHUB_TOKEN`.
- **Distinct `AUTH_SESSION_SECRET` per lane** — a dev/qa session token must not replay in prod.
- `AUTH_ENABLED` is pinned by the lane compose file (`false` on dev/qa, `true` on prod) and is not read from the env file.
- The prod lane additionally requires `COACH_WEB_IMAGE` (digest-pinned release image) and, because auth is enabled and fail-closed, the Google OIDC credentials.
- **`github-mcp` is digest-pinned on the prod lane** (`ghcr.io/github/github-mcp-server:v1.12.2@sha256:508a…cac6`, AC7 / #113); dev/qa stay tag-pinned. Refresh the digest with `docker buildx imagetools inspect ghcr.io/github/github-mcp-server:v1.12.2`.
- Prod lane OIDC: register redirect URI `http://localhost:8000/auth/callback` (and the `127.0.0.1` variant, or set `AUTH_REDIRECT_URI` explicitly — the app derives different redirect URIs for the two hostnames).

**Create a lane env file — `chmod 600` is mandatory (MADR-0008 C3):**

```bash
cp .env.dev.example .env.dev   # or .env.qa.example / .env.prod.example
chmod 600 .env.dev             # owner read/write only — never 644
```

The lane file carries `OPENROUTER_API_KEY`, `GITHUB_TOKEN`, `INTERVALS_API_KEY` and `AUTH_SESSION_SECRET`; a group/world-readable file leaks them to every local user on the host. `scripts/e2e-preflight.sh` asserts that **every existing lane env file is mode 600** and fails the gate with an actionable message otherwise. MADR-0008 C3 extends the same requirement to the future `DATA_ENCRYPTION_KEY` env-file secret (dedicated per-lane Fernet key, `chmod 600`, gitignored).

---

## 🐳 Local Lane Operations

### Prerequisites (one-time)

```bash
# GHCR pull access for the qa/prod lanes (PAT with read:packages scope)
docker login ghcr.io
```

### Start / stop a lane

```bash
# dev — build from source
docker compose -p coach-web-dev -f compose.yaml -f compose.dev.yml --env-file .env.dev up -d --build

# qa — pull the CI-built :qa image
docker compose -p coach-web-qa -f compose.yaml -f compose.qa.yml --env-file .env.qa pull
docker compose -p coach-web-qa -f compose.yaml -f compose.qa.yml --env-file .env.qa up -d

# prod — digest-pinned release image (COACH_WEB_IMAGE in .env.prod)
docker compose -p coach-web-prod -f compose.yaml -f compose.prod.yml --env-file .env.prod pull
docker compose -p coach-web-prod -f compose.yaml -f compose.prod.yml --env-file .env.prod up -d

# stop one lane (tears down only that project)
docker compose -p coach-web-qa -f compose.yaml -f compose.qa.yml --env-file .env.qa down
```

### Per-lane pre-flight (release gate)

```bash
./scripts/e2e-preflight.sh dev   # or qa / prod
```

The pre-flight asserts: every existing lane env file is **mode 600** (MADR-0008 C3, #170), the **exact service set** (`coach-web`, `coach-mcp`, `github-mcp`) is present and healthy — it uses `docker compose ps -a`, so a crashed/exited sidecar fails the gate instead of being silently absent — coach-web is published on `127.0.0.1` only, sidecars have no published ports, and (prod) `/api/*` returns 401 without a session. It fails fast with a clear message when a lane image is not yet published to GHCR.

> **Precondition:** `ghcr.io/fpittelo/coach-web:qa` exists only after the first post-change qa-lane build (a `workflow_dispatch` on `main` with `environment=qa`; same for the `coach` repo's `coach-mcp:qa` sidecar tag). The runbook below states when each pull becomes possible.

### Local lane lifecycle (`scripts/lane.sh`)

Agents (and humans) drive the dev/qa lanes through one entrypoint instead of hand-written compose invocations:

```bash
./scripts/lane.sh dev up              # build/start + wait until all three services are healthy
./scripts/lane.sh dev status          # ps -a for the lane's compose project only
./scripts/lane.sh dev logs coach-web  # --tail=100, optional service filter
./scripts/lane.sh dev down            # project-scoped teardown (--remove-orphans)
```

- `up` prints the lane URL (`http://127.0.0.1:8100` dev / `:8200` qa) and the per-service states once every service reports healthy.
- Scope is **dev/qa for lifecycle**: `prod up|down` are rejected with a pointer to systemd — the prod lane is supervised by `coach-web.service` (one supervisor only, MADR-0024; see *Boot & Supervision* below). `prod status` and `prod logs [service]` are read-only and supported.
- Every action is scoped to the lane's own compose project (`coach-web-<lane>`); concurrent lanes and unrelated compose projects are never touched.
- `scripts/e2e-preflight.sh` remains the validation gate — `lane.sh up` gets the lane running for browser-based verification, it does not replace the pre-flight assertions.

---

## 🔁 Boot & Supervision (prod lane, #328)

The prod lane (`coach-web-prod`) is supervised by a **systemd user unit** so it comes back by itself after a reboot — no manual `up -d` (MADR-0024). The unit is rendered from the repo-tracked template `deploy/systemd/coach-web.service.in`; the installed copy under `~/.config/systemd/user/` is **never hand-edited** — re-run the installer to refresh it.

What the unit actually recovers: the compose `up -d` runs once at boot (without a login, thanks to `linger`) and is re-run when the *unit* itself fails (`Restart=on-failure`, bounded by `StartLimit*`). It does **not** resurrect a container that dies later — the services declare no compose `restart:` policy, so a crashed container stays down until `systemctl --user restart coach-web`.

| Property | Value |
|:---|:---|
| Unit | `~/.config/systemd/user/coach-web.service` (`Type=oneshot`, `RemainAfterExit=yes`, `WantedBy=default.target`) |
| Auto-update | `coach-web-update.timer` + `coach-web-update.service` — installed by the same command (see *Auto-Update*) |
| Supervisor | `systemctl --user` — **one supervisor only**: `lane.sh prod up\|down` is refused with a pointer here |
| Boot without login | `loginctl enable-linger "$USER"` (required; the installer asserts it and refuses to install without it) |
| Docker race | bounded `ExecStartPre` retry (30 × 2 s) before `up`, with `TimeoutStartSec=1200` above it — docker is a *system* unit and a user unit cannot order against it |
| Checkout guard | the **first** `ExecStartPre` runs `scripts/repo-clean-check.sh`: the lane boots **only** from a clean checkout of `main` whose `HEAD` equals the last-known `origin/main`. A dirty or developer-branch tree fails the boot loudly instead of running unreviewed host-side code (#344) |
| Shared lock | start **and** stop take the auto-update lock (`/run/user/%U/coach-web-prod.lock`, C3) with `flock -w 900`: the lane waits for an in-flight update tick instead of racing it; `TimeoutStopSec=1200` so a stop can wait too |
| Secrets | `.env.prod` is passed to compose via `--env-file` only — never `EnvironmentFile=`, never printed, never copied (C4) |

### Install / refresh

```bash
./scripts/prod-install.sh
```

Idempotent: a second run rewrites nothing and re-asserts `enable --now`. The installer

1. asserts `loginctl show-user "$USER" -p Linger` is `yes` — otherwise it prints the exact remediation (`loginctl enable-linger "$USER"`) and exits non-zero;
2. renders `coach-web.service` and the auto-update pair (`coach-web-update.service` + `coach-web-update.timer`, #326) into `~/.config/systemd/user/`, runs `systemctl --user daemon-reload`, then `enable --now` on both the lane unit and the timer;
3. prints the **boot-persistent user-unit audit list** — linger widens the boot scope to *every* enabled user unit (security condition C7), so the list is recorded;
4. prints a verification block: unit and timer state, the next tick, `docker compose ps -a` for `coach-web-prod`, and `/healthz`.

### Verify / restart / stop

```bash
./scripts/lane.sh prod status                 # read-only: ps -a for coach-web-prod
./scripts/lane.sh prod logs coach-web         # read-only: --tail=100 (optional service)
systemctl --user status coach-web             # unit state
systemctl --user restart coach-web            # re-apply the compose up (bounded docker retry)
systemctl --user stop coach-web               # project-scoped down --remove-orphans

# 3/3 healthy + serving version
docker compose -p coach-web-prod -f compose.yaml -f compose.prod.yml --env-file .env.prod ps -a
curl -fsS http://127.0.0.1:8000/healthz
```

`systemctl --user stop coach-web` runs the unit's `ExecStop` (`down --remove-orphans`), so it leaves no orphan containers; named volumes are kept. A later `systemctl --user restart coach-web` (or `start`) restores the lane.

### Uninstall

```bash
./scripts/prod-install.sh --uninstall
```

Disables and removes all three units — the auto-update timer first (so no update can race the teardown), then the service (its `ExecStop` downs the project). The data volume `coach-web-prod_coach-data` is **never** removed — `docker compose down` keeps named volumes and the script never passes `-v`.

### Troubleshooting

| Problem | Cause | Fix |
|:---|:---|:---|
| **`docker daemon not ready`** in `journalctl --user -u coach-web` | The unit started before the system `docker.service` was up and the 60 s readiness retry expired | `systemctl --user restart coach-web` (retries the wait); if it persists, check `systemctl status docker`, then re-run `./scripts/prod-install.sh` |
| **Lane is up only while logged in** | `linger` is off | `loginctl enable-linger "$USER"` (the installer refuses to install without it) |
| **Unit runs an old checkout** | The checkout moved/renamed after install (the unit hardcodes the absolute repo path) | Re-run `./scripts/prod-install.sh` — it re-renders `@REPO_ROOT@` |
| **`systemctl --user` cannot connect** | No user manager in the session | Log in (or enable linger) so `systemd --user` is running |
| **Unit failed repeatedly** | `StartLimitBurst=3` in 300 s reached — systemd stopped retrying | `systemctl --user reset-failed coach-web`, fix the cause, `systemctl --user restart coach-web` |
| **`repo-clean-check` ERROR in `journalctl --user -u coach-web` or `-u coach-web-update`** | The shared checkout is on a feature branch, has tracked changes, or its `HEAD` differs from the last-known `origin/main` (#344) | `git -C <repo> checkout main && git -C <repo> pull --ff-only`, then `systemctl --user reset-failed coach-web && systemctl --user restart coach-web`; the update timer retries on its next tick |

---

## 🔄 Auto-Update (release-driven, #326)

Since v0.14.0 a tag is the whole rollout: `coach-web-update.timer` runs `scripts/prod-update.sh` every 5 minutes and the prod lane converges onto the released image with **no manual step**. The script is a thin shell around `scripts/prod_update.py`, which is unit-tested (`tests/test_prod_update.py`).

| Property | Value |
|:---|:---|
| Units | `~/.config/systemd/user/coach-web-update.timer` → `coach-web-update.service` (`Type=oneshot`), rendered from `deploy/systemd/coach-web-update.{service,timer}.in` |
| Cadence | 5 min after boot and after each completed run (`RandomizedDelaySec=30`) |
| Convergence | the digest of `ghcr.io/fpittelo/coach-web:prod`, and **only when** it equals the digest of the latest GitHub release tag (provenance gate, C1) |
| No-op cost | one registry digest resolution — no pull, no container restart |
| Order of operations | **checkout guard** → provenance → **pull the verified digest** → measure + snapshot the database → atomic `.env.prod` rewrite → recreate → gate. `.env.prod` is only touched once the image is local, so a registry failure leaves it byte-identical. |
| Gate | 3/3 services healthy ≤ 120 s **and** `/healthz` reporting the release version; otherwise automatic rollback to the previous digest |
| Snapshot | encrypted pre-swap dump (`python -m coach_web.maintenance backup`) in `~/.local/state/coach-web/snapshots/` (mode 600, last 4 kept) |
| Lock | `/run/user/<uid>/coach-web-prod.lock` (`$XDG_RUNTIME_DIR`, C3). Take it: the update tick, `e2e-preflight.sh prod`, `coach-web.service` start/stop/restart (via `flock -w 900`) and `prod-update.sh --rollback`/`--force`. Contention: the tick **skips** and exits 0 (nothing touched); the preflight **refuses** to start (exit 1); the unit and the operator commands **wait** up to 900 s, then fail. |
| State | `~/.local/state/coach-web/update-state.json` (mode 600): last run, outcome, consecutive-failure streak |
| Secret hygiene | `.env.prod` is only ever passed to compose as `--env-file`; the updater logs the public image digest and never the file, and never runs `docker compose config` (C4) |

### Checkout guard — why the tree must stay clean (#344)

The supervisor and the updater run **docker-group-privileged code straight from the developer working tree** (`@REPO_ROOT@`). During the v0.14.0 rollout the tree was checked out on a review-pending branch, so unreviewed host-side code performed the production swap. The checkout guard closes that supply-chain hole without a new component: `scripts/repo-clean-check.sh` is the first thing both the updater and the boot unit (`coach-web.service`, `ExecStartPre`) run.

It refuses unless **all three** conditions hold:

1. the path is a git work tree;
2. the working tree has **no tracked modifications** (`git status --porcelain --untracked-files=no` empty) — **untracked files are allowed**, they cannot change a tracked script;
3. the checkout is on **`main`** and `HEAD` equals the **last-known** `refs/remotes/origin/main`.

On any violation the guard prints one line naming the failed condition and the fix, the updater records `reason=checkout-guard` in `~/.local/state/coach-web/update-state.json`, and it exits non-zero **before any registry, pull or docker call** — the lane is untouched. Example:

```
[prod-update] [repo-clean-check] ERROR: checkout is on 'fix/342-snapshot-extraction', not 'main' — prod tooling requires a clean checkout of 'main' at origin/main in /home/frede/projects/HOME/coach-web; fix: git -C … checkout main && git -C … pull --ff-only
```

Fix: `git checkout main && git pull --ff-only` in the checkout. A checkout on a feature branch, a mid-rebase tree or a stale clone all fail the same way.

- **`--force` never bypasses the guard.** `--force` skips the no-op check and the C8 pause; it does **not** skip the checkout guard — unreviewed code must never run regardless of operator intent. `--rollback` may run from any *clean* state, but still requires a clean tree.
- **Residual window (stale ref).** The guard never fetches (a fetch is a network op). It compares `HEAD` to the **last-fetched** `origin/main`, so a checkout whose `origin/main` ref is stale could pass while a newer release exists; the normal workflow (`git pull --ff-only` on `main`) keeps the ref current, and a genuinely dirty/foreign tree is still refused. The ref refreshes on the next fetch/pull.
- **Boot impact.** A dirty tree now **fails the boot unit** (`StartLimitBurst=3` then systemd gives up) instead of starting the lane from unreviewed files. Keep the shared checkout clean — that is a deliberate availability-for-safety trade-off.

### Watch the automatic path

```bash
systemctl --user list-timers coach-web-update.timer
journalctl --user -u coach-web-update.service -n 30 --no-pager   # one block per tick
curl -fsS http://127.0.0.1:8000/healthz    # version == the release tag without its "v"
```

Every tick logs `[prod-update] …`: `no change: .env.prod already pins sha256:…` (nothing touched), `update complete: the prod lane is serving vX.Y.Z (sha256:…)`, or an abort with the reason. A failure **before** the swap leaves the lane untouched; a failure **after** it restores the previous digest and brings the previous image back.

### Force an update (also clears a C8 pause)

```bash
scripts/prod-update.sh --force                     # re-resolve, re-pull, re-apply even when unchanged
systemctl --user start coach-web-update.service    # same run the timer makes (honours the pause)
```

After **12 consecutive failures** the updater pauses instead of hammering the registry: the tick then logs `auto-update paused (C8)`, exits 0 and touches nothing. `--force` resumes it. The pause arrives in **as little as ~20 minutes**: `Restart=on-failure` re-runs a *failed* trigger (bounded to `StartLimitBurst=3` per `StartLimitIntervalSec=300`), so a broken registry burns three failures per 5-minute window; runs that complete (an update or a no-op) pace out one attempt per tick, i.e. the full ~1 h.

### Inspect the state

```bash
cat ~/.local/state/coach-web/update-state.json    # last_run, outcome, consecutive_failures
ls -l ~/.local/state/coach-web/snapshots/         # pre-swap encrypted snapshots (mode 600, last 4)
```

### Rollback (image)

Automatic when the post-swap gate fails. Manual:

```bash
scripts/prod-update.sh --rollback    # restores the digest in .env.prod.prev and re-applies it
curl -fsS http://127.0.0.1:8000/healthz
```

`--rollback` takes the same shared lock as the timer (C3) and needs no network — the previous image is already local. It rolls back the **image** only: data rollback restores the pre-swap snapshot (see *Database Maintenance → Encrypted backup / restore*); an image rollback never rolls back a schema.

### Disable / re-enable automatic updates

```bash
systemctl --user stop coach-web-update.timer           # pause the ticks (the lane keeps serving)
systemctl --user disable coach-web-update.timer        # keep it off across reboots
systemctl --user enable --now coach-web-update.timer   # resume
```

This is the supported way to keep one release off the lane (e.g. one carrying a **destructive migration**): roll back to the intended digest, stop the timer, then roll that release out manually (Release Runbook step 2, *fallback*).

> **Known limits (C2, C6).**
>
> * **Nothing unverified is ever deployed.** An unreachable registry, an unreachable or rate-limited GitHub API, an absent tag, a malformed digest, a failed `docker pull`, or an `.env.prod` that is not owner-only mode 600 aborts with `.env.prod` byte-identical and the lane untouched.
> * **Snapshot size.** The pre-swap artifact is staged on the container's 64 MB `/tmp` tmpfs and is ~4/3 of the raw database, so the effective ceiling is **~48 MB of database**. The updater measures the database first and aborts with `database too large for an in-container snapshot` beyond that (the lane keeps serving) — use the off-host `backup` path instead. A successful copy is also verified: an empty or truncated artifact is removed and the update aborts before `.env.prod` changes.
> * **Interrupted swap (F2).** `.env.prod` is rewritten *before* the lane is recreated, so a machine death in that window leaves `.env.prod` pinning a digest the lane is not yet running; the next tick then reports `no change` and does not recreate the lane by itself. Recovery: `scripts/prod-update.sh --force` (re-apply the pinned digest) or `scripts/prod-update.sh --rollback` (return to the previous release), then check `/healthz`.

---

## 💾 Database Maintenance (VACUUM + encrypted backup/restore, #274)

The coach database (`COACH_DB_PATH`, default `/data/coach.db` in the lane volume) is C2-classified health data (MADR-0008). Two maintenance commands ship in the `coach-web` image (`python -m coach_web.maintenance …`, run inside the container):

### VACUUM — reclaim deleted plaintext pages

Migration 0006 rewrote the debrief RPEs in place; the plaintext values they replaced persist in SQLite free space until a `VACUUM` rebuilds the file. Run after any encryption migration (and e.g. monthly):

```bash
# stop the lane first (VACUUM needs a brief exclusive lock)
./scripts/lane.sh dev down    # or the compose down command for qa/prod
docker compose -p coach-web-dev -f compose.yaml -f compose.dev.yml --env-file .env.dev \
  run --rm coach-web python -m coach_web.maintenance vacuum
./scripts/lane.sh dev up
```

**Verification:** `PRAGMA freelist_count` must be `0` afterwards — e.g.
`docker compose -p coach-web-dev … run --rm coach-web python -c "import sqlite3; print(sqlite3.connect('/data/coach.db').execute('PRAGMA freelist_count').fetchone())"`.

### Encrypted backup / restore

The backup artifact is a **Fernet-encrypted envelope around the SQLite file bytes** — encrypted at rest with the lane's `DATA_ENCRYPTION_KEY` (the same key the app and migration 0006 use; no second key). The key comes from the lane env file (`chmod 600`, never committed, never logged). The artifact is C2-derived: store it with the same care as the database itself. The artifact is written with owner-only `0600` permissions regardless of the process umask (#301).

**Artifact ↔ key version (#301):** each artifact is bound to the exact `DATA_ENCRYPTION_KEY` value that produced it — the artifact carries no key-version header. If the key is ever rotated (e.g. to a MultiFernet setup, the rotation mechanism MADR-0008 C4 specifies), keep every retired key in escrow for as long as its artifacts exist: `restore` can only decrypt an artifact with the key that produced it (a MultiFernet decrypts with the first member key that verifies), so date the artifact filenames — as the runbook below does — and record which key version wrote which artifacts before rotating.

**Backup target — non-co-location is a requirement (MADR-0008 C2):** the artifact must **never** share a volume or host with the live DB volume or with the `DATA_ENCRYPTION_KEY` escrow (the lane env file and its escrowed copies). The backup boundary is the one that actually matters (MADR-0008): a single lost disk or compromised host must not be able to reach both the key and the backups. Write the artifact to an off-host target or a protected separate volume — e.g. an encrypted external disk or network share mounted at `/mnt/coach-backups` — never to `/data`, the lane volume that holds both the database and the key env file.

```bash
# backup (lane stopped or idle; folds the WAL into the main file first)
# --output goes to a host directory mounted from a separate volume/host — never /data
# (the host directory must be writable by the container user, UID 10001)
docker compose -p coach-web-prod -f compose.yaml -f compose.prod.yml --env-file .env.prod \
  run --rm -v /mnt/coach-backups:/backup coach-web \
  python -m coach_web.maintenance backup --output /backup/coach-$(date +%F).backup
```

**Restore runbook (disaster recovery):**

1. **Recover the key from escrow (MADR-0008 C2 — explicit step, exercised in every restore rehearsal):** retrieve the lane's `DATA_ENCRYPTION_KEY` from the password manager (primary escrow) or the offline copy on its separate encrypted medium — never from the backup target — and confirm it decrypts a recent artifact by running the step-2 restore against its `--output` scratch path (`/data/coach-restored.db`) before touching the live volume.
2. Restore into a fresh database file:

```bash
docker compose -p coach-web-prod -f compose.yaml -f compose.prod.yml --env-file .env.prod \
  run --rm -v /mnt/coach-backups:/backup:ro coach-web \
  python -m coach_web.maintenance restore /backup/coach-2026-10-18.backup --output /data/coach-restored.db
```

3. Verify (row counts vs. the live DB, lane health-check, spot-check a recent debrief decryption), then swap the restored file in and start the lane.

- **Wrong/missing key → fail closed:** a restore with the wrong key fails with `maintenance failed: …` (Fernet `InvalidToken`) and leaves no half-written database behind; a backup without `DATA_ENCRYPTION_KEY` refuses to run (never writes plaintext).
- **Verification step (after every backup):** restore the artifact into a scratch path and confirm the row counts match the live DB before deleting the scratch file.
- **Schedule:** weekly `backup` (before the weekly `VACUUM`), keep at least the last 4 artifacts on the non-co-located target above; each artifact is only decryptable with the lane's `DATA_ENCRYPTION_KEY` — losing that key loses every backup, which is why key escrow and rehearsed recovery (step 1) are part of this runbook (MADR-0008 C2; restore rehearsed before the first prod release of the persistence story, re-rehearsed quarterly).

---

## 📌 Sidecar Image Pinning (coach-mcp, #275)

The `coach-mcp` sidecar (`ghcr.io/fpittelo/coach`) is built and published by a **separate pipeline** (the `coach` repository). Its lane tags (`:dev`, `:qa`, `:prod`) are mutable, so an upstream push would silently change the topology coach-web is tested against (the #248 incident class — a sidecar tool-schema change can break coach-web's transport assumptions with no coach-web commit).

**Pin:** each lane's compose override pins the sidecar by digest — `ghcr.io/fpittelo/coach@sha256:<digest>` — as the `${COACH_MCP_IMAGE:-…}` default, and the matching `.env.<lane>.example` mirrors it. The digest is the pin; the tag alone is not. `COACH_MCP_IMAGE` remains an optional override (e.g. to test a new sidecar build) — the compose default is the committed pin.

**Drift check:** `scripts/check_sidecar_drift.sh` resolves the current digest of each lane's floating tag and compares it to the pinned digest. It runs as a step in the CI **Compose Lane Validation** job and fails loudly (non-zero exit) on a mismatch, so a sidecar change is a deliberate act, not a silent one.

**Updating the pin deliberately:**

```bash
# 1. resolve the new upstream digest for the lane
docker buildx imagetools inspect ghcr.io/fpittelo/coach:dev

# 2. set the new digest in the lane's compose override AND its env template
#    (dev: compose.dev.yml + .env.dev.example; qa/prod likewise)

# 3. re-validate the drift check and the lane
./scripts/check_sidecar_drift.sh
./scripts/e2e-preflight.sh dev
```

`github-mcp` follows the same pattern (tag-pinned on dev/qa, digest-pinned on prod, AC7 / #113).

---

## 🚀 Release Runbook (approve + tag)

> **Automated rollout (MADR-0024, Sprint 29 #326/#328 — in force from v0.14.0):** tagging is the whole rollout. Within 5 minutes of the GitHub Release the prod lane resolves `:prod`, verifies its digest against the latest release tag, **pulls that verified digest**, snapshots the database, then atomically rewrites `COACH_WEB_IMAGE` and recreates the lane, and finally requires 3/3 healthy and `/healthz` on the release version — otherwise it restores the previous digest automatically. Step 2's manual block is the **fallback**, step 3 is **rollback**; the operational runbook for all of it is *Auto-Update* above.

1. **Staging validation** (release candidate on trunk `main`): dispatch the deploy workflow on `main` with `environment=qa` — CI builds and pushes `ghcr.io/fpittelo/coach-web:qa`. Then locally:
   ```bash
   docker compose -p coach-web-qa -f compose.yaml -f compose.qa.yml --env-file .env.qa pull
   ./scripts/e2e-preflight.sh qa
   docker compose -p coach-web-qa -f compose.yaml -f compose.qa.yml --env-file .env.qa up -d
   ```
   The pre-flight tears its lane down on exit, so the final `up -d` is what leaves the lane serving.

   **Intervals.icu CRUD gate** (pre-release, #311): with the lane serving, prove the full
   event write path through the coach-mcp sidecar — the gate creates, reads, updates and
   deletes one unmistakably-marked, future-dated (+7 days) test event via the sidecar's MCP
   tools (never direct Intervals.icu API calls), guarantees the delete even when a
   mid-lifecycle step fails, and fails loudly if an orphan test event remains. The
   `INTERVALS_API_KEY` stays inside the sidecar; the script needs no secret:
   ```bash
   docker run --rm --network coach-web-qa_coach-net \
       -v "$PWD:/workspace:ro" --entrypoint python \
       ghcr.io/fpittelo/coach-web:qa \
       /workspace/scripts/verify_icu_crud.py --mcp-url http://coach-mcp:8000/sse
   ```
   Expected output — one evidence line per lifecycle step, all `PASS`, exit code `0`:
   ```
   PASS create: event id=145755481
   PASS read: fields match (name, date)
   PASS update: title set to 'COACH-WEB RELEASE GATE — 2026-10-25 [UPDATED]'
   PASS read-after-update: fields match (name, date)
   PASS delete: event id=145755481
   PASS absent: event id=145755481 not listed
   GATE PASS
   ```
   Any `FAIL` line (or exit code `1`) blocks the release. On `FAIL absent: orphan event
   id=... still listed` the test event is still on the calendar — delete it manually in
   Intervals.icu (title prefix `COACH-WEB RELEASE GATE — `) before re-running. On
   `FAIL create:`/`FAIL connect:` the sidecar is unreachable or its upstream is failing —
   check `docker compose -p coach-web-qa -f compose.yaml -f compose.qa.yml --env-file .env.qa ps`
   and the sidecar healthcheck, then re-run. The same command shape runs against any lane
   (dev: `--network coach-web-dev_coach-net` with the `:dev` image).
2. **Release** (requires the single @fpittelo approval per release): tag `vX.Y.Z` on `main`. CI builds and pushes `:prod`, `:latest`, and the `vX.Y.Z` tag. **The rollout is then automatic** (MADR-0024) — verify it landed:
   ```bash
   systemctl --user list-timers coach-web-update.timer
   journalctl --user -u coach-web-update.service -n 20 --no-pager
   curl -fsS http://127.0.0.1:8000/healthz     # JSON version == X.Y.Z
   ```
   **Fallback / manual rollout** (automation disabled, or a release carrying a **destructive migration** — those are excluded from unattended update by design): set `COACH_WEB_IMAGE=ghcr.io/fpittelo/coach-web@sha256:<digest>` in `.env.prod`, then:
   ```bash
   systemctl --user stop coach-web-update.timer   # keep the timer out of the way
   docker compose -p coach-web-prod -f compose.yaml -f compose.prod.yml --env-file .env.prod pull
   ./scripts/e2e-preflight.sh prod
   docker compose -p coach-web-prod -f compose.yaml -f compose.prod.yml --env-file .env.prod up -d
   curl -fsS http://127.0.0.1:8000/health
   systemctl --user start coach-web-update.timer
   ```
   The pre-flight tears its lane down on exit, so the final `up -d` is what leaves the lane serving. Stopping the timer keeps the *tick* out of the way for the whole block; the pre-flight still takes the shared prod-lane lock itself (C3) and refuses to run while an update is in flight, and the manual `up -d` here is the one compose call that runs outside the lock — that is why the timer must stay stopped until it finishes.
3. **Rollback:** automatic when the post-swap gate fails (the updater restores the previous digest before exiting non-zero). Manual: `./scripts/prod-update.sh --rollback` — it restores the digest retained in `.env.prod.prev`, takes the shared prod-lane lock and re-applies the previous image; then re-check `/healthz` (see *Auto-Update → Rollback*). Data rollback restores the pre-swap encrypted snapshot (see *Database Maintenance → Encrypted backup / restore*) — image rollback never rolls back a schema. No cloud console involved.

---

## 🚦 Release Gate (v0.8.0)

The v0.8.0 release is gated on **all** of the following. No release tag may be cut until every item is green:

| # | Gate | Owner | Evidence |
|:---|:---|:---|:---|
| 1 | **All CI checks green** — `test`, `compose-validation`, `secret-scan` (gitleaks full history), `dependency-scan` (`pip-audit --strict`), **`image-scan` (trivy HIGH/CRITICAL)** | @developer / @devops | GitHub Actions run on the PR merged into `main` |
| 2 | **Pre-flight green on all three lanes** — `./scripts/e2e-preflight.sh dev`, `qa`, `prod` | @fpittelo | Local terminal output |
| 3 | **Manual E2E checklist signed off** — OIDC login → agent chat → tool calls → plan approval on the prod lane | @fpittelo | [`docs/e2e-checklist.md`](e2e-checklist.md), signed in the release issue |
| 4 | **Security audit report** — STRIDE + nLPD review recorded in the #113 closing comment | @cyber-security | Issue #113 closing comment |

> The trivy gate is **not** weakened to pass: a HIGH/CRITICAL finding is a real release blocker. If the base image carries an unfixed HIGH/CRITICAL, it is reported and triaged, not suppressed.

---

## 🧪 Manual E2E Verification (prod lane)

The automatable parts of the prod happy path (401 without a session, healthchecks, loopback publishing, service-set presence) are covered by `tests/` and `scripts/e2e-preflight.sh`. The credential-dependent happy path — **OIDC login → agent chat → tool calls → plan approval** — requires real Google, OpenRouter and Intervals.icu credentials and is verified manually by @fpittelo using the step-by-step checklist in [`docs/e2e-checklist.md`](e2e-checklist.md).

---

## 🏗️ Container Hardening (unchanged, all lanes)

| Control | Implementation |
|:---|:---|
| **Non-root user** | `coach-web` (`UID:GID 10001:10001`) |
| **Multi-stage build** | Builder stage installs deps; runtime stage is `python:3.12-slim` |
| **No shell access** | `ENTRYPOINT ["uvicorn", "coach_web.app:create_app", "--factory", …]` |
| **Read-only rootfs** | `read_only: true` + tmpfs `/tmp` (`noexec,nosuid`) |
| **Capability drop** | `cap_drop: ALL` + `no-new-privileges` |
| **Resource limits** | cpus 1.0 / mem 512M per container |
| **.dockerignore** | Excludes `.git`, `tests/`, `docs/`, `.venv/` |

---

## 🚀 CI/CD Pipeline

### `.github/workflows/ci.yaml`

| Step | Tool | Purpose |
|:---|:---|:---|
| Lint | `ruff check .` | Zero ruff warnings |
| Format | `black --check .` + `isort --check-only .` | Zero formatting violations |
| Types | `mypy --strict src/coach_web` | Zero type errors |
| Test | `pytest -W error --cov` | Zero failures, zero warnings |
| Security | `gitleaks` + `pip-audit --strict` | Zero secrets, zero known CVEs |
| Image scan | `trivy` (`image-scan` job) | Zero HIGH/CRITICAL OS/library CVEs (LOW/MEDIUM reported, non-failing) |
| Compose | `docker compose config --quiet` per lane | All three lane combinations validate (base + dev/qa/prod) |
| OpenTofu | `tofu fmt/validate` on `infra/` | **Removed with #110** (retired with the GCP IaC) |

### `.github/workflows/deploy.yaml`

- **Trigger:** push to `main`, `v*` tags, `workflow_dispatch` (`environment=qa` for staging validation)
- **Registry:** `ghcr.io/fpittelo/coach-web` — lane tags `dev`, `qa`, `prod`, `latest`, `<sha>`, `vX.Y.Z`
- **Sidecar verification:** the digest-resolution step proves `ghcr.io/fpittelo/coach:<lane>` and the digest-pinned `github-mcp-server` release resolve before a lane image is declared good
- **GCP deploy stage:** removed by #110 (ADR-007); the single @fpittelo release approval anchors the tag-from-trunk release (MADR-0017)

---

## 🌳 Branch Lifecycle

Coach Web follows the HOME Governance trunk+tag lifecycle (MADR-0017):

1. **`main`** — the single persistent trunk: all feature branches merge here via squash PR, and releases are cut from it as `vX.Y.Z` tags
2. **`dev` / `qa`** — retired at the #286 cutover; there are no promotion branches and no promotion PRs

### Local Pre-Flight Gate

Before pushing a feature branch, run:

```bash
ruff check . && black --check . && isort --check-only . && mypy --strict src/coach_web && pytest -v --cov=src/coach_web tests/
```

---

## 📦 GitHub Container Registry (GHCR)

| Trigger | Tag |
|:---|:---|
| Push to `main` | `coach-web:dev`, `coach-web:<sha>` |
| `workflow_dispatch` (`environment=qa`) on `main` | `coach-web:qa`, `coach-web:<sha>` |
| `v*` tag on `main` | `coach-web:latest`, `coach-web:prod`, `coach-web:<sha>`, `coach-web:vX.Y.Z` |

The qa/prod lanes pull these CI-built images — a local build for qa/prod would bypass the zero-warning CI gate (ADR-007).

---

## 📊 Monitoring & Health

| Check | URL |
|:---|:---|
| **App health** | `http://localhost:<lane-port>/health` (status, service, version, uptime) |
| **App liveness** | `http://localhost:<lane-port>/healthz` |
| **coach-mcp sidecar** | SSE listener on the lane network (`http://coach-mcp:8000/sse`) — HTTP healthcheck in compose |

---

## 🔧 Troubleshooting

| Problem | Cause | Fix |
|:---|:---|:---|
| **qa/prod lane pull fails** | Lane image not yet published (first qa-lane build pending) or no GHCR auth | Check the lane build ran; `docker login ghcr.io` with a `read:packages` PAT |
| **Blank dashboard / agent errors** | coach-mcp sidecar unhealthy | `docker compose -p coach-web-<lane> ps -a` — check healthchecks; verify `INTERVALS_API_KEY` |
| **Pre-flight fails on service set** | A sidecar crashed or never started | `docker compose -p coach-web-<lane> ps -a` and `logs`; the gate uses `ps -a` so exited containers are visible |
| **Plans empty** | GitHub token missing or wrong scope | Verify `GITHUB_TOKEN` reaches the sidecars with the lane's env file |
| **Prod lane 401 loop on login** | OIDC redirect URI mismatch | Register both `localhost` and `127.0.0.1` redirect URIs, or set `AUTH_REDIRECT_URI` |
| **Port already in use** | A single-stack from before #109 is still running | Tear down the legacy project once: `docker compose -p coach-web down` (then remove the stale root `.env`) |
| **CI fails on mypy** | Missing type annotations | Run `mypy --strict src/coach_web` locally and fix all errors |
| **CI `image-scan` fails** | HIGH/CRITICAL CVE in the image | Triage the trivy table; bump the base image/dependency. Do **not** lower the severity threshold |

---

_Last updated: 2026-10-25 (issue #344 — checkout guard: the updater and the boot unit refuse to run from a dirty/foreign/stale checkout (`scripts/repo-clean-check.sh`, `ExecStartPre`), recording `reason=checkout-guard` before any docker call; issue #326 — new "Auto-Update" section: the release-driven, provenance-gated `coach-web-update.timer` runbook (auto path, force, state inspection, rollback, disable/re-enable) + the C1–C8 limits, the swap order (verified pull, then snapshot, then the atomic rewrite) and the pre-swap snapshot; the C3 lock story is now explicit — the tick, `e2e-preflight.sh prod`, `coach-web.service` start/stop and `--rollback`/`--force` all take it, with skip/refuse/wait semantics and the ~20 min vs ~1 h C8 bound; `prod-install.sh` now also installs/removes the update service+timer; corrected the #328 advisory A1 wording so "Boot & Supervision" states what the unit actually recovers — boot and unit-level failures, not a crashed container, since there is no compose `restart:` policy; prior: issue #328 — "Boot & Supervision" section for the systemd user-unit supervision of the prod lane per MADR-0024: install/verify/restart/stop/uninstall + "docker daemon not ready" troubleshooting; `lane.sh` now exposes read-only `prod status`/`prod logs`; issue #327 — Release Runbook rollout automated per MADR-0024: tag-driven auto-update is the normal path, manual block demoted to fallback, rollback rewritten around `.env.prod.prev` + pre-swap snapshot; issue #301 — maintenance CLI hardening)_
