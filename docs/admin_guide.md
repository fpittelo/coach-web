# 📘 Admin Guide — Coach Web

**Audience:** @devops, @architect, @fpittelo  
**Last updated:** 2026-11-14

> **Deployment model: ADR-007 — Local-First** (`docs/architecture.md` §10). The local Docker host on the workstation is the deployment target for the dev, qa, and prod lanes. The Sprint 08 GCP topology was retired (#64–#68, decommissioned via #111) and is **reinstated as the Cloud Run production lane per MADR-0027** — see ☁️ GCP Cloud Run Production below.

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
| No-op cost | one registry digest resolution **plus** one lane health check (`docker compose ps -a`) — no pull, no container restart; a healthy lane is never recreated (#345) |
| Order of operations | **checkout guard** → provenance → **pull the verified digest** → measure + snapshot the database → atomic `.env.prod` rewrite → recreate → gate. `.env.prod` is only touched once the image is local, so a registry failure leaves it byte-identical. |
| Gate | 3/3 services healthy ≤ 120 s **and** `/healthz` reporting the release version; otherwise automatic rollback to the previous digest |
| Lane health | #345: every tick — a no-change tick included — verifies the exact 3/3 set; a missing or unhealthy service triggers **one** bounded self-heal (`up -d --remove-orphans`, project-scoped, lock held, no image pull). See *Lane health & self-heal* below. |
| Snapshot | encrypted pre-swap dump (`python -m coach_web.maintenance backup`) in `~/.local/state/coach-web/snapshots/` (mode 600, last 4 kept) |
| Lock | `/run/user/<uid>/coach-web-prod.lock` (`$XDG_RUNTIME_DIR`, C3). Take it: the update tick, `e2e-preflight.sh prod`, `coach-web.service` start/stop/restart (via `flock -w 900`) and `prod-update.sh --rollback`/`--force`. Contention: the tick **skips** and exits 0 (nothing touched); the preflight **refuses** to start (exit 1); the unit and the operator commands **wait** up to 900 s, then fail. |
| State | `~/.local/state/coach-web/update-state.json` (mode 600): last run, outcome, consecutive-failure streak, failed-run reason, and the #345 `lane` signal (`healthy`/`recovered`/`degraded`) |
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
cat ~/.local/state/coach-web/update-state.json    # last_run, outcome, lane, consecutive_failures
ls -l ~/.local/state/coach-web/snapshots/         # pre-swap encrypted snapshots (mode 600, last 4)
```

### Lane health & self-heal (#345)

During the v0.14.0 rollout `github-mcp` disappeared **after** the swap gate had passed 3/3. The lane then served **2/3 for ~20 minutes unnoticed**, because the no-change branch of the updater returned before it ever looked at the lane: it recorded `outcome=success` on the strength of the digest alone, and `coach-web.service` is `Type=oneshot`+`RemainAfterExit=yes`, so it converges only at boot and never re-converges a container removed at runtime.

Every tick now measures the lane, a no-change tick included. The state file records a `lane` field next to the outcome:

| `lane` | Meaning | What happened |
|:---|:---|:---|
| **`healthy`** | 3/3 services present and healthy | Nothing was touched — no pull, no recreate |
| **`recovered`** | The set was incomplete/unhealthy and the self-heal fixed it | One `up -d --remove-orphans` recreated the missing service(s) — **no image was pulled** |
| **`degraded`** | The lane is still not 3/3 after the self-heal (or the `up` itself failed) | The tick exits 0 so the timer keeps trying; the lane needs human eyes |

Where to see it:

```bash
cat ~/.local/state/coach-web/update-state.json                     # "lane": "healthy|recovered|degraded"
journalctl --user -u coach-web-update.service -n 30 --no-pager    # LANE RECOVERED / LANE DEGRADED lines
```

What the self-heal **does** and **does not** do:

- It re-runs the same idempotent, project-scoped convergence command the boot path uses (`docker compose -p coach-web-prod … up -d --remove-orphans`), so it recreates only what is missing. On a complete healthy lane `up -d` is a config-hash no-op: **a healthy lane is never disturbed** (`lane=healthy`, zero `up` calls).
- It holds the same shared prod-lane lock as every other prod operation (C3) and runs **inside** the tick — there is no new supervisor, timer, unit or cron.
- It **never pulls an image** and never rewrites `.env.prod`: it cannot change the running version, only restore the service set the pinned image defines.
- It does **not** run when the digest differs — that path already does the full gated update (pull → snapshot → rewrite → recreate → gate).
- A `degraded` lane is recorded as `outcome=degraded`, **never** `success`, and does **not** accumulate the C8 failure streak (a paused updater would stop before its lane check and hide the degradation). A lane that stays `degraded` across ticks is **not** self-healing and needs human eyes: inspect `journalctl --user -u coach-web-update.service` and `docker compose -p coach-web-prod … ps -a`, then fix and re-run `up -d` (`scripts/prod-update.sh --force` re-applies the pinned digest).

The image rollback below is the automatic response to a **failed gate**; the lane self-heal above is the automatic response to a **healthy gate whose lane later lost a service**. They are complementary.

### Rollback (image)

Automatic when the post-swap gate fails. Manual:

```bash
scripts/prod-update.sh --rollback    # restores the digest in .env.prod.prev and re-applies it
curl -fsS http://127.0.0.1:8000/healthz
```

`--rollback` takes the same shared lock as the timer (C3). It rolls back the **image** only: data rollback restores the pre-swap snapshot (see *Database Maintenance → Encrypted backup / restore*); an image rollback never rolls back a schema. **Network note (observed in the #329 drill):** if the previous digest was pruned locally, the rollback pulls it from the registry again — the "already local" assumption does not always hold; the pull is graceful and the rollback still completes, but allow for registry reachability.

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

## ☁️ GCP Cloud Run Production (MADR-0027)

> **Model:** always-on production, scale-to-zero, IAP edge gate (MADR-0027, Option 3). Project `coach-web-509209`, all resources pinned `europe-west6` (Swiss nLPD). Deploys run from GitHub Actions over Workload Identity Federation (keyless — no SA JSON keys anywhere). This section is the **one-time bootstrap** (#315) plus the IAP wiring that the deploy lane (#316) consumes.

### Constants

| Item | Value |
|:---|:---|
| Project ID | `coach-web-509209` |
| Project number | `853194930683` |
| Region | `europe-west6` |
| WIF pool | `github-pool` (global) |
| WIF provider | `github-oidc-provider` (issuer `https://token.actions.githubusercontent.com`) |
| Deploy SA (workflow identity) | `coach-web-deployer@coach-web-509209.iam.gserviceaccount.com` |
| Runtime SA (service identity — Amendment 0001) | `coach-web-runtime@coach-web-509209.iam.gserviceaccount.com` |
| IAP service agent | `service-853194930683@gcp-sa-iap.iam.gserviceaccount.com` |
| Image | `ghcr.io/fpittelo/coach-web` (anonymously pullable — no image-pull secret needed) |

### One-time bootstrap (#315 — executed 2026-11-14)

Re-executable on a fresh project, in this order; every create is check-first (describe → create only if absent).

```bash
PROJECT_ID=coach-web-509209
SA_EMAIL=coach-web-deployer@coach-web-509209.iam.gserviceaccount.com
POOL=github-pool
PROVIDER=github-oidc-provider
PROJECT_NUMBER=853194930683

# 1. Required APIs (sts for WIF token exchange was the only gap on 2026-11-14)
gcloud services enable sts.googleapis.com iam.googleapis.com iamcredentials.googleapis.com \
  secretmanager.googleapis.com run.googleapis.com iap.googleapis.com --project=$PROJECT_ID

# 2. WIF pool + GitHub OIDC provider (keyless — AC3)
gcloud iam workload-identity-pools create $POOL \
  --location=global --project=$PROJECT_ID \
  --display-name="GitHub Actions OIDC pool"
gcloud iam workload-identity-pools providers create-oidc $PROVIDER \
  --location=global --workload-identity-pool=$POOL --project=$PROJECT_ID \
  --display-name="GitHub OIDC fpittelo/coach-web" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref,attribute.sha=assertion.sha" \
  --attribute-condition="assertion.repository=='fpittelo/coach-web'"

# 3. Deploy/runtime SA + least-privilege bindings
gcloud iam service-accounts create coach-web-deployer \
  --project=$PROJECT_ID \
  --display-name="Coach Web deployer (GitHub Actions WIF)"
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" --role="roles/run.admin" --condition=None
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" --role="roles/serviceusage.serviceUsageConsumer" --condition=None
# self-actAs: the deploy sets the service's runtime identity (same SA)
gcloud iam service-accounts add-iam-policy-binding $SA_EMAIL \
  --member="serviceAccount:$SA_EMAIL" --role="roles/iam.serviceAccountUser"
# keyless path: only this repository's OIDC tokens may act as the SA
gcloud iam service-accounts add-iam-policy-binding $SA_EMAIL \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.repository/fpittelo/coach-web" \
  --role="roles/iam.workloadIdentityUser"

# 4. Secret Manager shells (payload pinned europe-west6; per-secret IAM only)
for SECRET in OPENROUTER_API_KEY GOOGLE_OIDC_CLIENT_SECRET AUTH_SESSION_SECRET GITHUB_TOKEN DATA_ENCRYPTION_KEY INTERVALS_API_KEY; do
  gcloud secrets create "$SECRET" --project=$PROJECT_ID \
    --replication-policy=user-managed --locations=europe-west6
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --project=$PROJECT_ID \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/secretmanager.secretAccessor"
done
```

Bootstrap notes:

- **Replication:** the single-location `--location=europe-west6` regional-secret create is broken in gcloud 593.0.0 (`INVALID_ARGUMENT: The provided Project ID ... does not match the expected format [projects/*]`); the user-managed single-replica form above is the equivalent residency pin — secret payload stored/replicated **only** in `europe-west6`.
- **Role ID:** the deploy SA role is `roles/serviceusage.serviceUsageConsumer` (`roles/iam.serviceUsageConsumer` does not exist).
- **AC3 verification:** `gcloud iam service-accounts keys list --iam-account=$SA_EMAIL --managed-by=user` → empty. The SA's `SYSTEM_MANAGED`/`GOOGLE_PROVIDED` signing key is normal and is not a user-managed JSON key.
- **Amendment 0001:** the commands above are the historical #315 record (executed 2026-11-14, when deploy SA = runtime SA). The AC5 security review on #316 re-scoped the identity model — apply the one-time deltas in **"One-time bootstrap — Amendment 0001 deltas"** below before the first dispatch; on a fresh project, run this bootstrap first, then the deltas.

### One-time bootstrap — Amendment 0001 deltas (AC5 fix round, #316 — PO-executed, not yet applied)

MADR-0027 Amendment 0001 (2026-11-15; cyber BLOCK on #316 — HIGH 8.1, "deploy SA = runtime SA") splits the deploy and runtime identities and narrows WIF to trunk. The deltas below are **one-time, PO-executed Cloud Shell steps** (GCP-side execution is PO-routed by design; agents never touch the GCP console/CLI). They must be applied **before the first dispatch** — the cyber re-verification of the delta precedes any live deploy (AC5 gate on #316).

```bash
PROJECT_ID=coach-web-509209
DEPLOY_SA=coach-web-deployer@coach-web-509209.iam.gserviceaccount.com
RUNTIME_SA=coach-web-runtime@coach-web-509209.iam.gserviceaccount.com
POOL=github-pool
PROVIDER=github-oidc-provider

# 1. Runtime SA — the service identity. ONLY the six per-secret accessor
#    bindings (no invoker, no IAP, no IAM-mutation roles: it is the service
#    identity, NOT an invoker — unauthenticated access stays fail-closed at
#    the IAP edge).
gcloud iam service-accounts create coach-web-runtime \
  --project=$PROJECT_ID \
  --display-name="Coach Web runtime (service identity)"
for SECRET in OPENROUTER_API_KEY GOOGLE_OIDC_CLIENT_SECRET AUTH_SESSION_SECRET GITHUB_TOKEN DATA_ENCRYPTION_KEY INTERVALS_API_KEY; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --project=$PROJECT_ID \
    --member="serviceAccount:$RUNTIME_SA" \
    --role="roles/secretmanager.secretAccessor"
done

# 2. actAs bridge — the deploy sets the service's runtime identity, so the
#    deployer needs roles/iam.serviceAccountUser ON the runtime SA.
gcloud iam service-accounts add-iam-policy-binding $RUNTIME_SA \
  --project=$PROJECT_ID \
  --member="serviceAccount:$DEPLOY_SA" \
  --role="roles/iam.serviceAccountUser"

# 3. Pre-flight metadata viewer — PER-SECRET (metadata only, values never
#    accessed). Supersedes the project-level roles/secretmanager.viewer
#    routing that was provisionally documented for #316:
for SECRET in OPENROUTER_API_KEY GOOGLE_OIDC_CLIENT_SECRET AUTH_SESSION_SECRET GITHUB_TOKEN DATA_ENCRYPTION_KEY INTERVALS_API_KEY; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --project=$PROJECT_ID \
    --member="serviceAccount:$DEPLOY_SA" \
    --role="roles/secretmanager.viewer"
done
# 3b. Remove the project-level viewer routing IF it was already applied
#     (if it was never granted, skip this — the removal errors otherwise):
gcloud projects remove-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$DEPLOY_SA" --role="roles/secretmanager.viewer"

# 4. Least-privilege hygiene — the deployer no longer reads secret PAYLOADS
#    (the #315 accessor grants are superseded by the runtime SA's):
for SECRET in OPENROUTER_API_KEY GOOGLE_OIDC_CLIENT_SECRET AUTH_SESSION_SECRET GITHUB_TOKEN DATA_ENCRYPTION_KEY INTERVALS_API_KEY; do
  gcloud secrets remove-iam-policy-binding "$SECRET" \
    --project=$PROJECT_ID \
    --member="serviceAccount:$DEPLOY_SA" \
    --role="roles/secretmanager.secretAccessor"
done

# 5. WIF narrowed to trunk — deploys authenticate only from main
#    (post-squash-merge, MADR-0017), never from feature branches or forks:
gcloud iam workload-identity-pools providers update $PROVIDER \
  --location=global --workload-identity-pool=$POOL --project=$PROJECT_ID \
  --attribute-condition="assertion.repository=='fpittelo/coach-web' && assertion.ref=='refs/heads/main'"

# 6. Smoke ID-token minting — the deployer's SELF OpenIdTokenCreator grant
#    (delta 6, PO-executed 2026-11-15; the broader tokenCreator role was
#    added alongside and then removed — the end state is the narrow role only):
gcloud iam service-accounts add-iam-policy-binding coach-web-deployer@coach-web-509209.iam.gserviceaccount.com \
  --project=$PROJECT_ID \
  --member="serviceAccount:coach-web-deployer@coach-web-509209.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountOpenIdTokenCreator"
```

Delta notes:

- **Order matters:** step 1 (runtime SA + its six accessor bindings) must precede the first deploy — the service cannot resolve its secrets otherwise; steps 2–5 complete the bridge and hygiene before the first dispatch.
- **The runtime SA has NO invoker/IAP roles:** it is the service identity, not an invoker. Invocation rights stay with the IAP service agent (`roles/run.invoker`), and platform-edge access stays with the PO account + the deploy SA (smoke identity, `roles/iap.httpsResourceAccessor`).
- **Deployer keeps:** `roles/run.admin` (**project-level at Amendment 0001; re-scoped to resource-level on the coach-web service by Amendment 0002 — see the dedicated section below**), `roles/serviceusage.serviceUsageConsumer`, self-`roles/iam.serviceAccountUser`, self-`roles/iam.serviceAccountOpenIdTokenCreator` (delta 6 — smoke ID-token minting via the direct IAM Credentials API), repo-scoped `roles/iam.workloadIdentityUser`, `roles/iap.admin`, service-scoped `roles/iap.httpsResourceAccessor` (deploy-SA smoke identity) — and, after the deltas, per-secret `roles/secretmanager.viewer` (metadata) instead of payload access.
- **Delta 6 (smoke ID-token minting — PO-executed 2026-11-15):** a keyless WIF session cannot use `gcloud auth print-identity-token --audiences` (that flag requires classic SA-key credentials — live failure on deploy attempt #5); gcloud's `--impersonate-service-account` path is no alternative either — it first drives `generateAccessToken`, i.e. the broader `roles/iam.serviceAccountTokenCreator` (live PERMISSION_DENIED on deploy attempt #7), which the least-privilege self-grant deliberately excludes (cyber C1). The smoke step therefore calls the IAM Credentials API `generateIdToken` method directly, which requires exactly `getOpenIdToken` = `roles/iam.serviceAccountOpenIdTokenCreator` on the target SA — the self-grant bounds the minting power to the deploy identity itself. (Both roles were added during the delta; the broad tokenCreator was then removed — the end state is the narrow role only.)
- **Verification:** the runtime SA holds no project-level roles; its only grants are the six per-secret `secretAccessor` bindings (`gcloud secrets get-iam-policy <SECRET>` shows exactly `secretAccessor` for the runtime SA + `viewer` for the deployer).

### One-time bootstrap — Amendment 0002 deltas (deploy-SA least-privilege, #390 — @devops-executed 2026-11-21)

MADR-0027 Amendment 0002 (2026-11-21; @architect ruling on #390) re-scopes the deploy SA's `roles/run.admin` from **project-level** to **resource-level on the coach-web service** (the lowest grantable resource per https://docs.cloud.google.com/iam/docs/roles-permissions/run) — the blast radius shrinks from the project's entire Cloud Run estate to the one service it deploys. The deltas below were executed live by @devops on 2026-11-21 (before/after policy evidence on the #390 PR); on a fresh project, run them after the Amendment 0001 deltas:

```bash
PROJECT_ID=coach-web-509209
DEPLOY_SA=coach-web-deployer@coach-web-509209.iam.gserviceaccount.com
SERVICE=coach-web

# 1. Resource-scoped run.admin on the coach-web service (Amendment 0002 D2):
gcloud run services add-iam-policy-binding $SERVICE \
  --region=europe-west6 --project=$PROJECT_ID \
  --member="serviceAccount:$DEPLOY_SA" --role="roles/run.admin" --condition=None

# 2. Remove the project-level run.admin binding (no orphaned project grant —
#    add the resource-scoped binding FIRST so the deploy surface is never left
#    without run.admin; `--condition=None` is required to remove the
#    unconditional binding in non-interactive mode):
gcloud projects remove-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$DEPLOY_SA" --role="roles/run.admin" --condition=None
```

Amendment 0002 delta notes:

- **Deployer keeps (end state):** resource-scoped `roles/run.admin` on the coach-web service (Amendment 0002 D2), `roles/serviceusage.serviceUsageConsumer`, self-`roles/iam.serviceAccountUser`, self-`roles/iam.serviceAccountOpenIdTokenCreator` (delta 6 — smoke ID-token minting via the direct IAM Credentials API), repo-scoped `roles/iam.workloadIdentityUser`, project-level `roles/iap.admin` (Amendment 0002 D3 — KEEP, revisit trigger: a second Cloud Run service in the project), service-scoped `roles/iap.httpsResourceAccessor` (deploy-SA smoke identity), and per-secret `roles/secretmanager.viewer` (metadata) instead of payload access.
- **D1 (actAs bridge) and D3 (`iap.admin` scope) are KEEP decisions** — the actAs-on-runtime grant is structurally required to set `serviceAccountName` via `gcloud run services replace` (https://docs.cloud.google.com/run/docs/configuring/service-accounts); only the `run.admin` scope changes.
- **Bootstrap-from-zero residual:** `run.services.create` is only grantable at project level — re-creating a **deleted** service (the emergency off-switch above) requires a one-time PO re-grant of `roles/run.admin` at project level (or PO-executed recreation), then re-running the two deltas above.
- **Workflow consumption:** the deploy lane verifies the end state — the D4 drift guard asserts the serving `serviceAccountName` and the 6-secret per-container `secretKeyRef` wiring post-deploy, and an IAM-surface gate fails the run if the deploy SA holds any project-level `run.admin`-surface permission (`TestIamPermissions` probe — the deploy SA cannot read the project policy itself, since `roles/run.admin` does not include `resourcemanager.projects.getIamPolicy`).

### Amendment 0002 delta addendum — deploy-lane restoration (#406/#407, @devops-executed 2026-11-21)

The first deploy after the Amendment 0002 re-scope (run 44894171347) failed fail-closed at the *Ensure IAP invoker and edge-access bindings* step: `gcloud iap web add-iam-policy-binding` performs a **project-level Resource Manager lookup** (`resourcemanager.projects.get`) that was previously supplied by the removed project-level `roles/run.admin` — resource-scoped grants cannot supply project-level permissions, and `roles/iap.admin` covers only the `iap.*` surface (evidence: #407 comment 7061525094; ruling: #407 comment 7061551551).

**(a) Grant (executed live 2026-11-21, before/after policy evidence on the PR):**

```bash
gcloud projects add-iam-policy-binding coach-web-509209 \
  --member="serviceAccount:coach-web-deployer@coach-web-509209.iam.gserviceaccount.com" \
  --role="roles/resourcemanager.viewer" --condition=None
# (--condition=None is required in non-interactive mode: the project policy
# contains conditional bindings from the WIF bootstrap.)
```

**Why `roles/resourcemanager.viewer`:** the predefined Resource Manager Viewer role is **read-only across the hierarchy** — per the official role definition (https://docs.cloud.google.com/iam/docs/roles-permissions/resourcemanager) it holds `resourcemanager.projects.get`/`list`, `resourcemanager.folders.get`/`list`, `resourcemanager.organizations.get`, `resourcemanager.boundaries.get`/`list`, `resourcemanager.boundaryConfigs.get`, `resourcemanager.capabilities.get`, `resourcemanager.capabilityConfigs.get`/`list`, `resourcemanager.tagKeys.get`/`list`, `resourcemanager.tagValues.get`/`list`, `resourcemanager.tagHolds.list` (16 permissions): read-only project/folder/org **plus boundary/capability/tag metadata** reads — **no `projects.getIamPolicy`/`setIamPolicy`, zero write surface, zero `run.*` permissions**. (Earlier revisions of this section said "exactly `resourcemanager.projects.get` + `projects.list`" — corrected in #420 AC2: the role is broader than the two permissions the lane consumes and still entirely non-mutating.) It restores only the project metadata lookup every `--project`-scoped gcloud command needs and cannot mutate anything or read IAM policies — the Amendment 0002 least-privilege invariant holds (no project-level `run.admin`; the workflow's IAM-surface gate is unaffected since the role grants no `run.*` permission). Rejected alternatives (#407 ruling): a custom single-permission role (YAGNI — Google-maintained predefined role adds only read-only metadata), `roles/viewer` (far broader), re-adding project-level `run.admin` (re-opens the #390 finding).

**Deployer project-binding end state (Amendment 0002 + addendum):**

| Role | Scope | Source |
|:---|:---|:---|
| `roles/iap.admin` | project-level | Amendment 0001 (D3 KEEP) |
| `roles/serviceusage.serviceUsageConsumer` | project-level | Amendment 0001 |
| `roles/resourcemanager.viewer` | project-level | **Amendment 0002 delta addendum (#407)** |
| `roles/run.admin` | coach-web **service resource** only | Amendment 0002 D2 |
| `roles/iam.serviceAccountUser` on runtime SA / self `iam.serviceAccountOpenIdTokenCreator` / repo-scoped `iam.workloadIdentityUser` / per-secret `secretmanager.viewer` | as before | Amendment 0001 deltas |

> **Runtime-SA surface (not a project binding):** the deployer additionally holds `roles/iam.serviceAccountViewer` **on `coach-web-runtime` only** — the C4 gate's runtime-SA policy read (#420; see *Standing deploy-lane grants* below).

**(b) C3 correction — dual-URL serving topology (#406 ruling comment 7061551550):** Cloud Run serves the service under **two URL families** — the primary per-service hash form (`status.address.url`, e.g. `coach-web-afff35tqxq-oa.a.run.app`) AND the deterministic project-number form `coach-web-853194930683.europe-west6.run.app`. The #396 C3 implementation enumerated only the scraped primary form, so requests arriving with the project-number Host failed `TrustedHostMiddleware` with 400 "Invalid host header". The workflow now renders:

- `TRUSTED_HOSTS` = the **exact two-host list** `["<primary-hash-host>","<service>-<project-number>.<region>.run.app"]` — exact-match posture preserved, **no wildcard** in steady state (the `'["*.run.app"]'` bootstrap fallback stays confined to the render before the service exists); the C2/C3 read-back gate asserts the same two-host list (compact JSON separators — the value is bash-built).
- `CORS_ORIGINS` = **both** origins (primary + project-number form).
- `AUTH_REDIRECT_URI` unchanged — pinned to the **primary** form (single canonical OIDC callback; Google requires exact `redirect_uri` match). The app-level canonical-host redirect middleware (#406 part 2, separate PR) sends any non-canonical-host request to the primary form, keeping the OIDC `state` cookie host-scoping intact.

### Standing deploy-lane grants (#420 — cyber-verified, record of the applied state)

The deploy lane holds exactly **two standing read-only grants** beyond the Amendment 0001/0002 body. They were applied live during the deploy-lane restoration, verified by @cyber-security on 2026-11-22, and are recorded in decision form as **MADR-0027 Amendment 0003**. Evidence chain: verification request [#406 comment 7075977132](https://github.com/fpittelo/coach-web/issues/406#issuecomment-7075977132), runtime-SA policy state [#406 comment 7075976714](https://github.com/fpittelo/coach-web/issues/406#issuecomment-7075976714), deploy-run discharge chain [#407 comment 7075976280](https://github.com/fpittelo/coach-web/issues/407#issuecomment-7075976280) — verdict recorded in **#420** (Grant 1 **APPROVE**, Grant 2 **APPROVE-WITH-CONDITIONS**, conditions documentation-only).

| Grant | Role → member, scope | Permissions (official role definition) | Why the lane needs it | Verdict |
|:---|:---|:---|:---|:---|
| **G1** | `roles/resourcemanager.viewer` → `coach-web-deployer`, **project** `coach-web-509209` | 16 read-only: `resourcemanager.projects.get`/`list`, `folders.get`/`list`, `organizations.get`, `boundaries.get`/`list`, `boundaryConfigs.get`, `capabilities.get`, `capabilityConfigs.get`/`list`, `tagKeys.get`/`list`, `tagValues.get`/`list`, `tagHolds.list` — **no `getIamPolicy`/`setIamPolicy`, no write, no `run.*`** | the `gcloud iap web` step's project-level Resource Manager lookup (removed with project-level `run.admin`, Amendment 0002) | **APPROVE** |
| **G2** | `roles/iam.serviceAccountViewer` → `coach-web-deployer`, **on the runtime SA** `coach-web-runtime@…` only | 9 read-only: `iam.serviceAccountKeys.get`/`list`, `iam.serviceAccounts.get`, `iam.serviceAccounts.getIamPolicy`, `iam.serviceAccounts.list`, `iam.serviceAccounts.listEffectiveTags`, `iam.serviceAccounts.listTagBindings`, `resourcemanager.projects.get`/`list` | the **C4 gate** reads the runtime SA's policy on every deploy to assert "no token-minting role on the runtime identity" | **APPROVE-WITH-CONDITIONS** (doc-only, discharged here + Am.0003) |

```bash
# G2 — resource-scoped on the runtime SA only (never project-level):
gcloud iam service-accounts add-iam-policy-binding \
  coach-web-runtime@coach-web-509209.iam.gserviceaccount.com \
  --project=coach-web-509209 \
  --member="serviceAccount:coach-web-deployer@coach-web-509209.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountViewer"
# G1 — the project-level binding is the command in (a) above.
```

- **G2's public-key-metadata qualification:** `iam.serviceAccountKeys.get`/`list` return **public key material and key metadata only** (key id, algorithm, origin/type, validity, `disabled`). No API returns private key bytes — a user-managed key's private material exists only in the JSON handed to the creator at creation and is retrievable by no role. The lane holds **no user-managed keys** (keyless WIF; `keys list --managed-by=user` empty, #315 AC3), so no private-key path exists.
- **G2 explicitly excludes** `iam.serviceAccounts.getAccessToken`, `getOpenIdToken`, `signBlob`, `signJwt` (all minting), `actAs`, and `setIamPolicy`: the deployer cannot mint credentials for the runtime identity, cannot impersonate it, and cannot mutate its policy. The only actAs path stays the Amendment 0002 D1 bridge required to attach the service identity.
- **Fail-closed, not degraded:** without G1 the run aborts at the IAP-binding step (run 44894171347); without G2 it aborts at the C4 gate (run 44897761116) — both PERMISSION_DENIED, before any traffic moves. The grants buy the reads the gates need, never a mutation surface; the deployment target state (runtime SA holds no minting binding) remains asserted live on every deploy.
- **⛔ Standing trigger — re-review before the next deploy:** if Google ever expands **either** predefined role toward token minting (`getAccessToken`/`getOpenIdToken`/`signBlob`/`signJwt`), `setIamPolicy`, `actAs`, or any write permission, the grant must be **replaced** (a resource-scoped custom role carrying exactly the permissions enumerated above) **or removed before another deploy**. A role expansion must never silently widen the deploy identity.

### PO secret fill (one-time, after bootstrap)

The 6 secrets are empty shells (0 versions). Fill each exactly once — values are never echoed and never committed:

```bash
# interactive stdin — value never lands in shell history or logs
gcloud secrets versions add OPENROUTER_API_KEY --data-file=-
# (paste the value, then Ctrl-D)
```

Repeat for `GOOGLE_OIDC_CLIENT_SECRET`, `AUTH_SESSION_SECRET`, `GITHUB_TOKEN`, `DATA_ENCRYPTION_KEY`, `INTERVALS_API_KEY` (console alternative: Secret Manager → secret → **Add new version**). Verify presence only:

```bash
for SECRET in OPENROUTER_API_KEY GOOGLE_OIDC_CLIENT_SECRET AUTH_SESSION_SECRET GITHUB_TOKEN DATA_ENCRYPTION_KEY INTERVALS_API_KEY; do
  gcloud secrets versions list "$SECRET"   # version 1 enabled — value is never displayed
done
```

### IAP prerequisites — brand + consent screen (console-only; MADR-0027 D2)

The legacy `gcloud iap oauth-brands` path is dead for this project: it requires an organization, and the IAP OAuth Admin APIs were shut down (2026-03-19). The one-time IAP prerequisite is therefore console-based (no-org projects cannot create OAuth clients programmatically):

1. **Branding / consent screen:** console → Google Auth Platform → **Branding** (`console.cloud.google.com/auth/branding`) → *Get started* → app name `Coach Web`, support email `frederic.pitteloud@gmail.com`, audience **External** → *Create*.
2. **OAuth client:** console → Google Auth Platform → **Clients** → *Create client* → type **Web application**, with authorized redirect URIs:
   - `https://iap.googleapis.com/v1/oauth/clientIds/CLIENT_ID:handleRedirect` (IAP edge)
   - `https://<service-url>/auth/callback` (app-level OIDC — the `.run.app` URL is fixed in #316)

   Record the client ID; the client **secret** is filled into the `GOOGLE_OIDC_CLIENT_SECRET` shell (step above).
3. **Apply the custom OAuth client to IAP** (required for gcloud-managed access grants in no-org projects):
   ```bash
   # iap_settings.yaml — NEVER commit this file (carries the client secret)
   access_settings:
     oauth_settings:
       client_id: CLIENT_ID
       client_secret: CLIENT_SECRET
   gcloud iap settings set iap_settings.yaml --project=$PROJECT_ID
   ```

> **Programmatic access (Google IAP requirement):** the custom OAuth client must have the **programmatic access allowlist** configured for ID-token-based programmatic access to the IAP-protected service — without it, authorized smoke requests 403 at the edge (fail-closed, but an avoidable deploy failure). Reference: Google IAP "Programmatic access" docs (<https://cloud.google.com/iap/docs/sharing-oauth-clients#programmatic_access>).

### IAP + invoker bindings (#316 — requires the deployed service)

```bash
SERVICE=coach-web   # as deployed by #316
# IAP service agent must be able to invoke the service (else every request 403s at invocation)
gcloud run services add-iam-policy-binding $SERVICE \
  --region=europe-west6 \
  --member=serviceAccount:service-$PROJECT_NUMBER@gcp-sa-iap.iam.gserviceaccount.com \
  --role=roles/run.invoker
# platform-edge access: the PO account
gcloud iap web add-iam-policy-binding \
  --member=user:frederic.pitteloud@gmail.com \
  --role=roles/iap.httpsResourceAccessor \
  --region=europe-west6 --resource-type=cloud-run --service=$SERVICE
# platform-edge access: the WIF deploy SA (post-deploy smoke checks; cyber-reviewed in #316)
gcloud iap web add-iam-policy-binding \
  --member=serviceAccount:$SA_EMAIL \
  --role=roles/iap.httpsResourceAccessor \
  --region=europe-west6 --resource-type=cloud-run --service=$SERVICE
```

### Federated deploy-time auth (#316 workflow snippet)

```yaml
- uses: google-github-actions/auth@v2
  with:
    project_id: coach-web-509209
    workload_identity_provider: projects/853194930683/locations/global/workloadIdentityPools/github-pool/providers/github-oidc-provider
    service_account: coach-web-deployer@coach-web-509209.iam.gserviceaccount.com
```

The binding chain (provider condition → repo-scoped principalSet → `roles/iam.workloadIdentityUser`) was verified at bootstrap (#315 evidence) under the original repo-only condition; the Amendment 0001 deltas narrow the provider condition to `assertion.repository=='fpittelo/coach-web' && assertion.ref=='refs/heads/main'` — deploys authenticate only from trunk, never from feature branches or forks. The first Actions deploy in #316 exercises the live token exchange end-to-end.

### Production deploy lane (#316 — manual workflow)

`.github/workflows/cloud-run-deploy.yaml` — `workflow_dispatch` with `image_tag_or_digest`, now **required, no default (#411)**: pass the **current trunk digest** (`sha256:…`), the `dev` tag, or a trunk-build sha tag; the input is resolved to an exact `@sha256:…` via `docker buildx imagetools inspect` before deploy — the pinned digest, not a mutable tag, is what ships. A resolution-time guard **refuses release-class tags** (`prod`, `latest`, `v*`) and fails the run before any registry resolution or Cloud Run mutation: they resolve to the **LAST RELEASE build** in GHCR, not the current trunk artifact — the former `prod` default resolved to the stale v0.20.0 digest and caused **three live prod incidents** (deploy runs 44897761116 and 44898471542 — the #406 forensics — and 45003393040, the 2026-11-22 `Invalid OAuth state` recurrence after login; issue #411). Example dispatch:

```bash
gh workflow run cloud-run-deploy.yaml --ref main \
  -f image_tag_or_digest="$(docker buildx imagetools inspect ghcr.io/fpittelo/coach-web:dev | sed -n 's/^Digest:[[:space:]]*//p')"
```

Every run passes the `gcp-cloud-run` GitHub Environment gate → one explicit @fpittelo approval per deploy (Amendment 0001: the whole pipeline — pre-flight included — is that single gated job; no step authenticates to GCP outside it). The **first execution (AC1) waits for the @cyber-security AC5 review** recorded on the #316 PR (secret wiring/masking, `FORWARDED_ALLOW_IPS=*` rationale, IAP enablement + both `iap.httpsResourceAccessor` grants) **and for the Amendment 0001 deltas above to be applied**.

One-time prerequisites beyond the #315 bootstrap — run before the first dispatch:

- **GitHub Environment gate:** the `gcp-cloud-run` environment must exist with `@fpittelo` as a **required reviewer** (repo → Settings → Environments → New environment → add required reviewer) — it is the one-approval-per-deploy gate; create it BEFORE the first dispatch so the very first run is gated.

```bash
# IAP enablement + per-service IAP grants are executed BY the workflow
# (MADR-0027 D2) — the deploy SA needs the IAP admin surface:
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" --role="roles/iap.admin"
# the pre-flight secret checks read per-secret METADATA only — the per-secret
# roles/secretmanager.viewer bindings, the deploy/runtime identity split and
# the WIF trunk condition are the Amendment 0001 deltas (see the dedicated
# section above; the former project-level roles/secretmanager.viewer routing
# is superseded and must not be applied):
gh variable set GOOGLE_OIDC_CLIENT_ID --repo fpittelo/coach-web <CLIENT_ID>
```

Deployed-pod contract (Cloud Run v2, 3 containers sharing one localhost namespace):

- **coach-web** (ingress container): the CI-built GHCR digest, non-root UID 10001 (baked in the image), binds `$PORT` (8080) via `APP_PORT`; `FORWARDED_ALLOW_IPS=*` (the GFE/IAP front end is the only ingress path — the baked Dockerfile default stays `127.0.0.1,::1` for the local lanes, #112/#316 AC2); the leftmost `X-Forwarded-For` value is never used for authorization or rate-limiting decisions (a client-supplied XFF entry is attacker-controlled — the IAP-authenticated edge is the only trust boundary); `TRUSTED_HOSTS` pinned to the exact two served hostnames (`["<service-hash>.a.run.app","<service>-<project-number>.<region>.run.app"]` — the primary hash form AND the deterministic project-number form, both served URL families enumerated explicitly, no wildcard, #406 ruling; the wildcard is only the bootstrap render fallback before the service exists and is replaced by the first-deploy sync); `CORS_ORIGINS` carries both origins and `AUTH_REDIRECT_URI` is pinned to the primary hash-form origin (canonical OIDC callback, #406) (auto-synced after a first deploy — the URL is knowable only once the service exists); `AUTH_ENABLED=true`, `AUTH_WHITELIST_EMAILS='["frederic.pitteloud@gmail.com"]'`; startup probe `GET /healthz` (cold-start-tolerant `initialDelaySeconds`) — the platform probe sends a **non-public Host** (not `*.run.app`), so the app middleware exempts exactly `/healthz` and `/health` from trusted-host validation (probe-only, #316); host validation remains fail-closed on every other route; note the exemption exposes version/uptime on `/health{,z}` to any in-pod caller (low-value fingerprint, cyber N1 #387).
- **Request timeout 3600 s** (service `timeoutSeconds`; the Cloud Run default 300 s would cut long agent SSE streams). **Bound:** a single chat turn — one request including its full SSE agent stream — must complete within 3600 s; turns beyond that bound are cut by the platform.
- **coach-mcp**: `ghcr.io/fpittelo/coach:prod` resolved to its current digest; `localhost:8000/sse`; **startup TCP socket probe only** (never HTTP against the SSE stream).
- **github-mcp**: pinned `v1.12.2@sha256:508a0857…` (same constant as `deploy.yaml`); `localhost:8001`; startup TCP socket probe.
- **Secret wiring (per-secret scoping, Amendment 0001):** coach-web → `OPENROUTER_API_KEY`, `GOOGLE_OIDC_CLIENT_SECRET`, `AUTH_SESSION_SECRET`, `DATA_ENCRYPTION_KEY` (the `GITHUB_TOKEN` mount was removed from the ingress container — the PAT is exposed **only** to github-mcp, as `GITHUB_PERSONAL_ACCESS_TOKEN`); coach-mcp → `INTERVALS_API_KEY` only; github-mcp → the `GITHUB_TOKEN` secret exposed as `GITHUB_PERSONAL_ACCESS_TOKEN` only. All references resolve at instance start (`latest`); the runtime identity is the dedicated `coach-web-runtime` SA (Amendment 0001 split — only the six per-secret `secretAccessor` bindings, no invoker/IAP roles; the deploy step sets it via `serviceAccountName` in the service YAML).
- **Persistence (MADR-0027 D3, amended by Amendment 0004 / MADR-0032):** in-memory tmpfs volume at `/data` (`emptyDir: medium: Memory` — SQLite WAL runs locally, never on `gcsfuse`; earlier references to "MADR-0021" were a numbering typo — 0021 is the portfolio agent-roster ADR). Currently per-instance scratch on the serving revision; under MADR-0032 (#416, rollout tracked in #417), `/data/coach.db` rehydrates at boot from, and checkpoints WAL-safe whole-file Fernet-encrypted snapshots (`DATA_ENCRYPTION_KEY`) to, a dedicated regional `EUROPE-WEST6` GCS bucket (`UBLA=enabled`, `PAP=enforced`, Versioning `numNewerVersions=2` + `daysSinceNoncurrentTime=7` + 7-day Soft Delete, fail-closed anti-clobber restore, bucket-scoped 3-permission custom runtime role `{storage.objects.create,delete,get}` and 2-permission deployer reader `{storage.buckets.get,getIamPolicy}` + `resourcemanager.projectIamViewer`). `minInstances=0`, `maxInstances=1`, region `europe-west6` everywhere.

Smoke checks executed by the workflow (IAP-aware — no PO browser needed; full 401/403 matrix stays in #317):

1. Unauthenticated `GET /` → **IAP 302/403 at the edge** (the app is never reached; an HTTP 200 fails the run).
2. Authorized smoke with the **deploy-SA ID token** (`iap.httpsResourceAccessor`; audience = the IAP OAuth client ID, extracted from the IAP sign-in redirect) and **no app session cookie**: `/health` + `/healthz` → 200; `/api/*` → 401 (app-layer boundary intact under IAP).
3. The live `.run.app` URL, the resolved digests and the serving revision are emitted in the job summary.

**Deploy-gate config assertion — `TRUSTED_HOSTS` = the exact two served hostnames (#396 C3, corrected per #406):** Cloud Run serves the service under two URL families — the primary per-service hash form (`coach-web-afff35tqxq-oa.a.run.app`, scraped from `gcloud run services describe … --format='value(status.address.url)'`) AND the deterministic project-number form (`coach-web-853194930683.europe-west6.run.app`, derived from `${SERVICE}-${GCP_PROJECT_NUMBER}.${GCP_REGION}.run.app`) — neither is statically derivable together with the other, so the workflow scrapes the primary URL before rendering the service YAML and pins `TRUSTED_HOSTS='["<primary-host>","<number-host>"]'` in the deployed env. The shared `'["*.run.app"]'` wildcard — which matches every Cloud Run service of every GCP project — remains **only** as the bootstrap fallback for the render that happens before the service exists (first deploy: no URL to scrape yet); the first-deploy sync step (which already replaces the placeholder `CORS_ORIGINS`/`AUTH_REDIRECT_URI`) now also replaces it with the exact two-host list before the smoke checks. The steady-state deploy path therefore always pins the exact two hosts, and a post-deploy read-back gate fails the run if the wildcard ever survives to the deployed service (#406: the original single-host exact match broke the project-number URL form — any request arriving with that Host 400'd "Invalid host header").

**Rollback** (runbook line — deliberately NOT automated in the workflow, grooming amendment #7): the pre-deploy serving revision is captured in the job summary; to roll back:

```bash
gcloud run services update-traffic coach-web --region=europe-west6 --to-revisions=<PREVIOUS_REVISION>=100
```

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

_Last updated: 2026-11-22 (issue #420 — **standing deploy-lane grants**, cyber-verified record: new "Standing deploy-lane grants" subsection documents both read-only grants with their exact permission sets, scopes, purposes and the fail-closed note — G1 `roles/resourcemanager.viewer` project-level → deployer (**APPROVE**; 16 read-only permissions, no `getIamPolicy`, no `run.*`) and G2 `roles/iam.serviceAccountViewer` **on the `coach-web-runtime` SA only** (**APPROVE-WITH-CONDITIONS**, documentation-only: `get`/`list`/`getIamPolicy` + public-key metadata, **no** token minting, **no** `actAs`, **no** `setIamPolicy`); the **standing trigger** is recorded — any future GCP expansion of either predefined role toward token minting/`setIamPolicy`/`actAs`/write requires grant replacement or removal before another deploy; G1's permission wording corrected (read-only project/folder/org **plus boundary/capability/tag metadata** reads, not "exactly `projects.get`/`list`"); evidence chain #406 comments 7075977132/7075976714 + #407 comment 7075976280, verdict recorded in #420, decision record MADR-0027 Amendment 0003. prior: issues #406/#407 — Amendment-0002 **delta addendum**, deploy-lane restoration: the deploy SA gains project-level `roles/resourcemanager.viewer` (#407 @architect ruling — the `gcloud iap web` step performs a project-level Resource Manager lookup previously supplied by the removed project-level `run.admin`; the role is read-only project/folder/org + boundary/capability/tag metadata, zero write surface, no `getIamPolicy`, executed live by @devops 2026-11-21 with before/after policy evidence on the PR; the #390 invariants stay intact — no project-level `run.admin`, the IAM-surface gate is unaffected since the role grants no `run.*` permission); the C3 deploy-gate config is corrected for the **dual-URL serving topology** (#406 @architect ruling comment 7061551550 — Cloud Run serves both the primary hash form and the deterministic project-number form): the workflow renders `TRUSTED_HOSTS` as the exact two-host list (no wildcard in steady state; bootstrap fallback confined), `CORS_ORIGINS` carries both origins, `AUTH_REDIRECT_URI` stays pinned to the primary hash-form origin (canonical OIDC callback; app-level canonical-host redirect middleware lands separately), and the C2/C3 read-back gate asserts the same two-host list. prior: issue #390 — MADR-0027 Amendment 0002, deploy-SA least-privilege: the deploy SA's `roles/run.admin` is re-scoped from project-level to **resource-level on the coach-web service** (new "One-time bootstrap — Amendment 0002 deltas" subsection with the resource-scoped add + project-level unbind commands, executed live by @devops 2026-11-21 with before/after policy evidence on the PR); the actAs-on-runtime bridge (D1) and the project-level `roles/iap.admin` scope (D3) are recorded KEEP decisions with an explicit revisit trigger; the workflow gains the D4 post-deploy drift guard (serving `serviceAccountName` == the runtime SA + the 6-secret per-container `secretKeyRef` surface unchanged — the invoker-set assertion is subsumed by #396 C1 and not duplicated) and a blocking IAM-surface gate asserting the deploy SA holds no project-level `run.admin`-surface permission via the permission-less `TestIamPermissions` probe; bootstrap-from-zero residual recorded: `run.services.create` is project-level only, so service recreation after the emergency off-switch needs a one-time PO re-grant. prior: issue #396 — deploy-gate IAM & config assertions implementing the #317 cyber sign-off conditions C1–C4: the verify step now also asserts the service `roles/run.invoker` member set is **exactly** the IAP service agent — a stray `allUsers`/`allAuthenticatedUsers`/other invoker fails the run (C1); a post-deploy read-back gate asserts the deployed `AUTH_REDIRECT_URI` is present, https and on the service origin (C2) and `TRUSTED_HOSTS` is the exact service hostname (C3 — the `'["*.run.app"]'` wildcard is only the bootstrap render fallback before the service exists, replaced by the first-deploy sync; corrected to the two-host served list by #406); a runtime-SA gate fails the run if any principal other than the platform Cloud Run service agent (`service-853194930683@serverless-robot-prod.iam.gserviceaccount.com`, Cloud Run's own runtime-identity mechanism) holds a token-minting role (`iam.serviceAccountTokenCreator` family) on `coach-web-runtime` (C4). prior: issue #316 — Amendment 0001 delta 6: the smoke step mints its ID token by a direct IAM Credentials API call (`generateIdToken` on the deployer SA) instead of `print-identity-token --audiences`, which a keyless WIF session cannot use, with a new drift guard asserting the scraped IAP client_id equals the `GOOGLE_OIDC_CLIENT_ID` Actions variable before minting; one-time PO self-grant `roles/iam.serviceAccountOpenIdTokenCreator` on the deployer SA — gcloud's `--impersonate-service-account` shortcut was rejected because it first drives `generateAccessToken` (the broader `roles/iam.serviceAccountTokenCreator`, deliberately not granted per cyber C1), while `generateIdToken` needs exactly the narrow `getOpenIdToken`. prior: issue #316 — startup-probe Host exemption: the platform startup probe sends a non-public Host, so the app middleware exempts exactly `/healthz` and `/health` from trusted-host validation (probe-only); host validation remains fail-closed on every other route. prior: issue #316 — AC5 fix round (MADR-0027 Amendment 0001): deploy/runtime identity split — the new `coach-web-runtime` runtime SA holds only the six per-secret `secretAccessor` bindings (no invoker/IAP roles), the deployer keeps the deploy/IAP surface plus per-secret `secretmanager.viewer` metadata grants and the `iam.serviceAccountUser` actAs bridge on the runtime SA, its six payload `secretAccessor` grants are removed, and the WIF provider condition is narrowed to repository + `refs/heads/main`; all GCP-authenticating workflow steps moved into the single `gcp-cloud-run` environment-gated job (one approval per deploy run); the `GITHUB_TOKEN` mount was removed from the coach-web ingress container (the PAT is only in github-mcp as `GITHUB_PERSONAL_ACCESS_TOKEN`) and `FORWARDED_ALLOW_IPS=*` is now set on the deployed coach-web env with the leftmost-XFF-never-for-authorization note; prior: issue #316 — new "Production deploy lane (#316 — manual workflow)" subsection: the `cloud-run-deploy.yaml` manual dispatch (digest-resolved `image_tag_or_digest`, `gcp-cloud-run` environment approval gate), the deploy-SA one-time prerequisites (`roles/iap.admin` for the in-workflow IAP enablement + grants, `roles/secretmanager.viewer` for the pre-flight versions list, the `GOOGLE_OIDC_CLIENT_ID` Actions variable), the deployed-pod contract (3-container pod, `FORWARDED_ALLOW_IPS=*` vs the loopback-only baked default, `TRUSTED_HOSTS=["*.run.app"]`, 3600 s request timeout as the max-chat-turn bound, in-memory `/data` per MADR-0021, min=0/max=1), the IAP-aware smoke semantics and the `update-traffic --to-revisions` rollback line. prior: issue #315 — new "GCP Cloud Run Production (MADR-0027)" section: the one-time bootstrap runbook reflecting the executed state — WIF pool `github-pool` + GitHub-OIDC provider `github-oidc-provider` (repo-condition-scoped), the `coach-web-deployer` SA with least-privilege bindings, the 6 empty Secret Manager shells pinned `europe-west6` with per-secret accessor IAM, the PO secret-fill step, the console-only IAP brand/consent/OAuth-client prerequisites (legacy `iap oauth-brands` API is dead for no-org projects) and the #316 IAP/invoker binding commands; the ADR-007 callout notes the Cloud Run production reinstatement per MADR-0027. prior: issue #329 — Auto-Update → Rollback: corrected the "no network / image already local" claim; the rollback pulls the previous digest from the registry when it was pruned locally (observed in the #329 drill), still lock-held and image-only. prior: issue #345 — per-tick lane health signal + bounded self-heal: every tick, a no-change tick included, verifies the exact 3/3 set (`lane-health`), makes ONE project-scoped `up -d` self-heal (lock held, no image pull) when a service is missing or unhealthy, and records `lane=healthy|recovered|degraded` additively in the state file (`outcome=degraded`, never `success`, when the lane does not recover); new "Lane health & self-heal" section. prior: issue #344 — checkout guard: the updater and the boot unit refuse to run from a dirty/foreign/stale checkout (`scripts/repo-clean-check.sh`, `ExecStartPre`), recording `reason=checkout-guard` before any docker call; issue #326 — new "Auto-Update" section: the release-driven, provenance-gated `coach-web-update.timer` runbook (auto path, force, state inspection, rollback, disable/re-enable) + the C1–C8 limits, the swap order (verified pull, then snapshot, then the atomic rewrite) and the pre-swap snapshot; the C3 lock story is now explicit — the tick, `e2e-preflight.sh prod`, `coach-web.service` start/stop and `--rollback`/`--force` all take it, with skip/refuse/wait semantics and the ~20 min vs ~1 h C8 bound; `prod-install.sh` now also installs/removes the update service+timer; corrected the #328 advisory A1 wording so "Boot & Supervision" states what the unit actually recovers — boot and unit-level failures, not a crashed container, since there is no compose `restart:` policy; prior: issue #328 — "Boot & Supervision" section for the systemd user-unit supervision of the prod lane per MADR-0024: install/verify/restart/stop/uninstall + "docker daemon not ready" troubleshooting; `lane.sh` now exposes read-only `prod status`/`prod logs`; issue #327 — Release Runbook rollout automated per MADR-0024: tag-driven auto-update is the normal path, manual block demoted to fallback, rollback rewritten around `.env.prod.prev` + pre-swap snapshot; issue #301 — maintenance CLI hardening)_
