# 📘 Admin Guide — Coach Web

**Audience:** @devops, @architect, @fpittelo  
**Last updated:** 2026-10-03

> **Deployment model: ADR-007 — Local-First** (`docs/architecture.md` §10). The local Docker host on the workstation is the deployment target for the dev, qa, and prod lanes. The GCP Cloud Run topology is retired (Sprint 08, #64–#68) and is decommissioned via #111.

---

## 🗺️ Deployment Model (ADR-007)

| Lane | Compose files | coach-web image | Host port | Auth | Purpose |
|:---|:---|:---|:---|:---|:---|
| **dev** | `compose.yaml` + `compose.dev.yml` | build from source | `127.0.0.1:8100` | `AUTH_ENABLED=false` | active development |
| **qa** | `compose.yaml` + `compose.qa.yml` | `ghcr.io/fpittelo/coach-web:qa` | `127.0.0.1:8200` | `AUTH_ENABLED=false` | staging validation |
| **prod** | `compose.yaml` + `compose.prod.yml` | digest-pinned `vX.Y.Z` | `127.0.0.1:8000` | `AUTH_ENABLED=true` (OIDC) | daily use |

- **All host bindings are `127.0.0.1` only** — loopback binding is the primary access control (ADR-007). Never bind `0.0.0.0`.
- Each lane runs as its own compose project (`-p coach-web-dev|qa|prod`) with its own network — lanes can run concurrently without port/name collisions.
- Sidecars (`coach-mcp`, `github-mcp`) publish no host ports; they are reachable only inside the lane network.
- Lane compose files are delivered by #109; until then the legacy single `docker-compose.yml` (project `coach-web`, port `127.0.0.1:8000`) remains the working topology.

---

## 📦 Environment Variables

`.env.example` is the single source of truth for the variable catalogue. Summary:

| Group | Variables | Notes |
|:---|:---|:---|
| **App** | `APP_HOST`, `APP_PORT`, `CORS_ORIGINS`, `LOG_LEVEL`, `CACHE_TTL_SECONDS`, `AGENT_MAX_TOOL_ITERATIONS` | uvicorn binding & app behaviour |
| **Auth (OIDC)** | `AUTH_ENABLED`, `GOOGLE_OIDC_CLIENT_ID`, `GOOGLE_OIDC_CLIENT_SECRET`, `GOOGLE_OIDC_ISSUER`, `AUTH_WHITELIST_EMAILS`, `AUTH_SESSION_SECRET`, `AUTH_SESSION_TTL_SECONDS`, `AUTH_REDIRECT_URI` | opt-in; fail-closed boot when enabled but misconfigured |
| **MCP sidecars** | `COACH_MCP_URL`, `GITHUB_MCP_URL`, `COACH_MCP_IMAGE`, `GITHUB_MCP_IMAGE` | compose service discovery (`http://coach-mcp:8000/sse`, `http://github-mcp:8001/`) |
| **GitHub** | `GITHUB_TOKEN`, `GITHUB_REPO`, `GITHUB_PLAN_BRANCH`, `GITHUB_PLAN_DIR`, `GITHUB_TOOLSETS` | PAT scope: `repo:read` (dev/qa) / read+write to plan branch (prod) |
| **OpenRouter** | `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, `OPENROUTER_MODEL`, `OPENROUTER_TIMEOUT_SECONDS` | coach agent LLM |
| **coach-mcp** | `INTERVALS_API_KEY`, `INTERVALS_ATHLETE_ID`, `INTERVALS_BASE_URL`, `MCP_TRANSPORT`, `MCP_HOST`, `MCP_PORT` | sidecar-local |

### Per-Lane Env Files (ADR-007, delivered with #109/#112)

- `.env.dev`, `.env.qa`, `.env.prod` — one per lane, **never a shared `.env`**; each is `chmod 600` and gitignored.
- **Distinct `AUTH_SESSION_SECRET` per lane** — a dev/qa session token must not replay in prod.
- **Per-service secret scoping:** `coach-web` receives only `OPENROUTER_API_KEY`, `GOOGLE_OIDC_CLIENT_SECRET`, `AUTH_SESSION_SECRET`, `GITHUB_TOKEN`; `coach-mcp` only `INTERVALS_API_KEY`; `github-mcp` only `GITHUB_TOKEN`.
- Prod lane OIDC: register redirect URI `http://localhost:8000/auth/callback` (and the `127.0.0.1` variant, or set `AUTH_REDIRECT_URI` explicitly — the app derives different redirect URIs for the two hostnames).

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
docker compose -p coach-web-dev -f compose.yaml -f compose.dev.yml up -d --build

# qa — pull the promoted :qa image
docker compose -p coach-web-qa -f compose.yaml -f compose.qa.yml pull
docker compose -p coach-web-qa -f compose.yaml -f compose.qa.yml up -d

# prod — digest-pinned release image
docker compose -p coach-web-prod -f compose.yaml -f compose.prod.yml pull
docker compose -p coach-web-prod -f compose.yaml -f compose.prod.yml up -d

# stop one lane (tears down only that project)
docker compose -p coach-web-qa -f compose.yaml -f compose.qa.yml down
```

### Per-lane pre-flight (release gate)

```bash
./scripts/e2e-preflight.sh dev   # or qa / prod
```

The pre-flight asserts: coach-web published on `127.0.0.1` only, sidecars have no published ports, all healthchecks healthy, and (prod) `/api/*` returns 401 without a session. It fails fast with a clear message when a lane image is not yet published to GHCR.

> **Precondition:** `ghcr.io/fpittelo/coach-web:qa` exists only after the first post-change `dev` → `qa` promotion (same for the `coach` repo's `coach-mcp:qa` sidecar tag). The runbook below states when each pull becomes possible.

---

## 🚀 Promotion Runbook (dev → qa → main)

1. **dev → qa** (requires @fpittelo approval on the promotion PR): merge `dev` into `qa` via PR. CI builds and pushes `ghcr.io/fpittelo/coach-web:qa`. Then locally: `docker compose -p coach-web-qa … pull && up -d` and run `./scripts/e2e-preflight.sh qa`.
2. **qa → main** (requires @fpittelo approval): merge `qa` into `main` via PR, then tag `vX.Y.Z` on `main`. CI builds and pushes `:prod`, `:latest`, and the `vX.Y.Z` tag; record the image digest in the release notes. Then locally: update the prod lane's pinned digest, `pull && up -d`, run `./scripts/e2e-preflight.sh prod`.
3. **Rollback:** `docker compose -p coach-web-prod … up -d` with the previous digest — no cloud console involved.

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
| OpenTofu | `tofu fmt/validate` on `infra/` | **Removed with #110** (retired with the GCP IaC) |

### `.github/workflows/deploy.yaml`

- **Trigger:** push to `dev`, PR closed into `qa`, `v*` tags, `workflow_dispatch`
- **Registry:** `ghcr.io/fpittelo/coach-web` — lane tags `dev`, `qa`, `prod`, `latest`, `<sha>`, `vX.Y.Z`
- **Sidecar verification:** the digest-resolution step proves `ghcr.io/fpittelo/coach:<lane>` and the pinned `github-mcp-server` tag resolve before a lane image is declared good
- **GCP deploy stage:** removed by #110 (ADR-007); the @fpittelo promotion approval lives in the promotion PRs

---

## 🌳 Branch Lifecycle

Coach Web follows the HOME Governance 3-branch lifecycle:

1. **`dev`** — active integration branch (all feature branches merge here via squash PR)
2. **`qa`** — staging branch (promoted from `dev` with @fpittelo approval)
3. **`main`** — production release branch (promoted from `qa` with @fpittelo approval)

### Local Pre-Flight Gate

Before pushing a feature branch, run:

```bash
ruff check . && black --check . && isort --check-only . && mypy --strict src/coach_web && pytest -v --cov=src/coach_web tests/
```

---

## 📦 GitHub Container Registry (GHCR)

| Trigger | Tag |
|:---|:---|
| Push to `dev` | `coach-web:dev`, `coach-web:<sha>` |
| PR merged to `qa` | `coach-web:qa`, `coach-web:<sha>` |
| `v*` tag on `main` | `coach-web:latest`, `coach-web:prod`, `coach-web:<sha>`, `coach-web:vX.Y.Z` |

The qa/prod lanes pull these CI-built images — a local build for qa/prod would bypass the zero-warning CI gate (ADR-007).

---

## 📊 Monitoring & Health

| Check | URL |
|:---|:---|
| **App health** | `http://localhost:<lane-port>/health` (status, service, version, uptime) |
| **App liveness** | `http://localhost:<lane-port>/healthz` |
| **coach-mcp sidecar** | SSE listener on the lane network (`http://coach-mcp:8000/sse`) — TCP healthcheck in compose |

---

## 🔧 Troubleshooting

| Problem | Cause | Fix |
|:---|:---|:---|
| **qa/prod lane pull fails** | Lane image not yet published (first promotion pending) or no GHCR auth | Check the promotion ran; `docker login ghcr.io` with a `read:packages` PAT |
| **Blank dashboard / agent errors** | coach-mcp sidecar unhealthy | `docker compose -p coach-web-<lane> ps` — check healthchecks; verify `INTERVALS_API_KEY` |
| **Plans empty** | GitHub token missing or wrong scope | Verify `GITHUB_TOKEN` reaches the sidecars with the lane's env file |
| **Prod lane 401 loop on login** | OIDC redirect URI mismatch | Register both `localhost` and `127.0.0.1` redirect URIs, or set `AUTH_REDIRECT_URI` |
| **Port already in use** | Legacy single-stack still running | Tear down the legacy project once: `docker compose -p coach-web down` |
| **CI fails on mypy** | Missing type annotations | Run `mypy --strict src/coach_web` locally and fix all errors |

---

_Last updated: 2026-10-03 (ADR-007 local-first refresh — issue #108)_
