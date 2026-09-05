# 📘 Admin Guide — Coach Web

**Audience:** @devops, @architect  
**Last updated:** 2026-09-05

---

## 📦 Environment Variables

| Variable | Description | Default | Required |
|:---|:---|:---|:---:|
| `COACH_MCP_URL` | URL of the Coach MCP server (streamable_http transport) | `http://localhost:8000/mcp` | ✅ |
| `GITHUB_TOKEN` | GitHub PAT with `repo:read` scope (for training plan issues) | — | ✅ |
| `GITHUB_REPO` | GitHub repo for training plans (`owner/name`) | `fpittelo/coach` | |
| `STREAMLIT_SERVER_PORT` | Port for the Streamlit server | `8501` | |
| `STREAMLIT_SERVER_ADDRESS` | Bind address | `0.0.0.0` | |
| `CACHE_TTL_SECONDS` | TTL for cached MCP/API responses | `60` | |
| `LOG_LEVEL` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` | |

### `.env.example`

```bash
# Coach MCP Server endpoint
COACH_MCP_URL=http://localhost:8000/mcp

# GitHub API (for training plan issues)
GITHUB_TOKEN=ghp_your_token_here
GITHUB_REPO=fpittelo/coach

# Streamlit
STREAMLIT_SERVER_PORT=8501
STREAMLIT_SERVER_ADDRESS=0.0.0.0

# Cache
CACHE_TTL_SECONDS=60

# Logging
LOG_LEVEL=INFO
```

---

## 🐳 Docker Deployment

### Build

```bash
docker build -t coach-web .
```

### Run (Stdio Mode — Local Dev)

```bash
docker run -d --rm \
  -p 8501:8501 \
  -e COACH_MCP_URL="http://host.docker.internal:8000/mcp" \
  -e GITHUB_TOKEN="ghp_xxx" \
  --name coach-web-app \
  coach-web
```

### Run with Docker Compose (Coach + Coach Web)

```yaml
# docker-compose.yml
version: '3.8'
services:
  coach-mcp:
    image: ghcr.io/fpittelo/coach:latest
    ports:
      - '8000:8000'
    environment:
      MCP_TRANSPORT: streamable_http
      MCP_PORT: '8000'
      INTERVALS_API_KEY: ${INTERVALS_API_KEY}
      INTERVALS_ATHLETE_ID: '0'

  coach-web:
    image: ghcr.io/fpittelo/coach-web:latest
    ports:
      - '8501:8501'
    environment:
      COACH_MCP_URL: http://coach-mcp:8000/mcp
      GITHUB_TOKEN: ${GITHUB_TOKEN}
      GITHUB_REPO: fpittelo/coach
    depends_on:
      - coach-mcp
```

```bash
docker compose up -d
```

---

## 🏗️ Container Hardening

The Dockerfile follows the same hardening pattern as the Coach MCP server:

| Control | Implementation |
|:---|:---|
| **Non-root user** | `coach-web` (`UID:GID 10001:10001`) |
| **Multi-stage build** | Builder stage installs deps; runtime stage is slim |
| **Base image** | `python:3.12-slim` |
| **No shell** | `ENTRYPOINT ["streamlit", "run", ...]` |
| **.dockerignore** | Excludes `.git`, `tests/`, `docs/`, `.venv/` |

### Dockerfile (Reference)

```dockerfile
FROM python:3.12-slim AS builder

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv pip install --system .

FROM python:3.12-slim

RUN groupadd -g 10001 coach-web && \
    useradd -u 10001 -g coach-web -s /sbin/nologin coach-web

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY src/ ./src/
COPY pyproject.toml ./

USER coach-web
EXPOSE 8501
ENTRYPOINT ["streamlit", "run", "src/coach_web/app.py", "--server.port", "8501", "--server.address", "0.0.0.0"]
```

---

## 🚀 CI/CD Pipeline

### `.github/workflows/ci.yaml`

| Step | Tool | Purpose |
|:---|:---|:---|
| Checkout | `actions/checkout@v4` | Clone repo |
| Setup Python | `actions/setup-python@v5` | Python 3.12 + pip cache |
| Install | `pip install .[dev]` | App + dev dependencies |
| Lint | `ruff check .` | Zero ruff warnings |
| Format | `black --check .` | Zero formatting violations |
| Imports | `isort --check-only .` | Zero import order violations |
| Types | `mypy --strict src/coach_web` | Zero type errors |
| Test | `pytest -v --cov=src/coach_web tests/` | Zero test failures, coverage report |
| Docker | `docker/build-push-action@v5` | Validate image builds |

### `.github/workflows/deploy.yaml`

- **Trigger:** Push to `dev`, merged PR to `qa`/`main`, tag `v*`, `workflow_dispatch`
- **Registry:** `ghcr.io/fpittelo/coach-web`
- **Tags:** `dev`, `qa`, `prod`, `latest`, `sha`, `vX.Y.Z`
- **Permissions:** `contents: read`, `packages: write`

---

## 🌳 Branch Lifecycle

Coach Web follows the HOME Governance 3-branch lifecycle:

1. **`dev`** — active integration branch (all feature branches merge here via squash PR)
2. **`qa`** — staging branch (promoted from `dev` with `@fpittelo` approval)
3. **`main`** — production release branch (promoted from `qa` with `@fpittelo` approval)

### Local Pre-Flight Gate

Before pushing a feature branch, run:

```bash
ruff check . && black --check . && isort --check-only . && mypy --strict src/coach_web && pytest -v --cov=src/coach_web tests/
```

---

## 📦 GitHub Container Registry (GHCR)

Pre-built images are published to `ghcr.io/fpittelo/coach-web`:

| Trigger | Tag |
|:---|:---|
| Push to `dev` | `coach-web:dev`, `coach-web:<sha>` |
| PR merged to `qa` | `coach-web:qa`, `coach-web:<sha>` |
| PR merged to `main` | `coach-web:latest`, `coach-web:prod`, `coach-web:<sha>` |

### Pull

```bash
docker pull ghcr.io/fpittelo/coach-web:latest
```

---

## 📊 Monitoring & Health

| Check | URL |
|:---|:---|
| **Streamlit health** | `http://localhost:8501/_stcore/health` |
| **MCP server health** | `http://localhost:8000/mcp` (MCP protocol handshake) |

---

## 🔧 Troubleshooting

| Problem | Cause | Fix |
|:---|:---|:---|
| **Blank dashboard** | MCP server not running | `curl $COACH_MCP_URL` — verify HTTP 200 |
| **Plans tab empty** | GitHub token missing or wrong scope | `curl -H "Authorization: token $GITHUB_TOKEN" https://api.github.com/repos/fpittelo/coach/issues` |
| **Container crash** | Permission denied | Ensure `UID:GID 10001:10001` exists in image |
| **CI fails on mypy** | Missing type annotations | Run `mypy --strict src/coach_web` locally and fix all errors |

---

_Last updated: 2026-09-05_