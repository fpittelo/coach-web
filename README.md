# 🚴‍♂️ Coach Web — Your Personal Endurance Cockpit

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE.md)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![MCP Client](https://img.shields.io/badge/MCP-Client-brightgreen.svg)](https://modelcontextprotocol.io)
[![Docker Non-Root](https://img.shields.io/badge/Docker-Non--Root%20(UID%2010001)-success.svg)](https://opencontainers.org)

> **Your coach talks. Your watts listen. Your body thanks you.**
>
> Coach Web is the Python-native FastAPI frontend for the Coach MCP server — turning your
> Intervals.icu biometric data into a live cockpit and your weekly training plans into a story
> you can scroll, not a spreadsheet you dread.

---

## 🎯 What Is This?

Coach Web is the **interactive frontend** for the [Coach MCP server](https://github.com/fpittelo/coach) — a FastMCP server that exposes the Intervals.icu REST API to LLMs via the Model Context Protocol.

Phase 1 (Local VIDAR Evolution) replaces the legacy Streamlit scaffold with an asynchronous
FastAPI core that serves a Swiss minimalist single-page interface (Alpine.js + self-hosted
Inter, zero CDN dependencies) and streams the agent pipeline over native Server-Sent Events.

The Phase 1 core delivers:

- **FastAPI application factory** with clean lifespan startup/shutdown
- **`GET /health` / `GET /healthz`** healthchecks (status, service, version, uptime)
- **Static asset serving** for the Swiss minimalist UI at `/static/`
- **Pydantic v2 settings** with `.env` loading
- **Dual MCP client hub** and OpenRouter agent loop streaming typed SSE events
- **Chat console** consuming `POST /api/agent/stream` via `fetch` + ReadableStream (client-owned multi-turn history, #79)
- **Plan approval card** posting to `POST /api/plan/approve` (Intervals.icu event + GitHub Markdown commit)

---

## 🏛️ Architecture in a Nutshell

```mermaid
graph TD
    subgraph "Your Browser"
        YOU(["🧑‍🚀 @fpittelo<br/>interacts via browser"])
    end

    subgraph "coach-web Container (OCI Non-Root UID 10001)"
        APP["FastAPI App<br/>app.py — Python 3.12<br/>⚡ async · static UI · SSE"]
    end

    subgraph "Coach MCP Server (already deployed)"
        MCP["FastMCP Server<br/>streamable_http :8000<br/>🔐 holds INTERVALS_API_KEY"]
    end

    subgraph "External APIs"
        INTERVALS["Intervals.icu REST API"]
        GITHUB["GitHub API<br/>training plan issues"]
    end

    YOU -->|"HTTP :8080"| APP
    APP -->|"MCP over HTTP"| MCP
    APP -->|"REST + token"| GITHUB
    MCP -->|"HTTPS Basic Auth"| INTERVALS
```

**Two containers. One language. Zero proxies.**

---

## 🚀 Quick Start

### Option 1: Local Dev (KIS)

```bash
# Clone
gh repo clone fpittelo/coach-web
cd coach-web

# Install with uv
uv sync

# Configure
cp .env.example .env
# Edit .env with your COACH_MCP_URL and GITHUB_TOKEN

# Run (FastAPI + uvicorn — binds APP_PORT, default 8000)
uv run coach-web
# or with autoreload on an explicit port override
uv run uvicorn coach_web.app:create_app --factory --reload --port 8080
```

Then open [http://localhost:8000](http://localhost:8000) (or `:8080` with the override above) — the Swiss minimalist shell is served from `/`.

Healthcheck: [http://localhost:8000/health](http://localhost:8000/health)

### Option 2: Docker (local lanes — ADR-007)

The stack runs on the local workstation in three isolated lanes (dev / qa / prod), all bound to `127.0.0.1` only:

```bash
# one-time per lane: create the lane env file from its template
cp .env.dev.example .env.dev   # then edit with your secrets

# dev lane — build from source
docker compose -p coach-web-dev -f compose.yaml -f compose.dev.yml \
    --env-file .env.dev up -d --build
```

Then open [http://localhost:8100](http://localhost:8100) (dev), `:8200` (qa), or `:8000` (prod) and meet your coach.

| Lane | Host port | Compose files | Env file |
|:---|:---|:---|:---|
| dev | `127.0.0.1:8100` | `compose.yaml` + `compose.dev.yml` | `.env.dev` |
| qa | `127.0.0.1:8200` | `compose.yaml` + `compose.qa.yml` | `.env.qa` |
| prod | `127.0.0.1:8000` | `compose.yaml` + `compose.prod.yml` | `.env.prod` |

Lanes run concurrently — each is its own compose project (`coach-web-dev|qa|prod`) with its own network, and sidecars publish no host ports. See the [Admin Guide](docs/admin_guide.md) for the full lane matrix, the promotion runbook, and the per-lane pre-flight (`./scripts/e2e-preflight.sh dev|qa|prod`).

---

## 📊 What You'll See

### Readiness Cockpit

| Metric | What it tells you |
|:---|:---|
| **FTP** | Your cycling power baseline (watts) — the anchor for every interval |
| **Resting HR** | Recovery quality — low = recovered, high = accumulated fatigue |
| **HRV (rMSSD)** | Nervous system readiness — higher = more parasympathetic = ready to train |
| **Sleep** | How much repair your body got last night |
| **CTL (Fitness)** | Your 42-day aerobic engine size — the bigger, the stronger |
| **ATL (Fatigue)** | Your 7-day acute load — the higher, the more tired you are right now |
| **TSB (Form)** | CTL minus ATL — negative = building, positive = tapering/peaking |

### Weekly Training Plans

The last 5 weeks + your current microcycle, pulled from GitHub issues (labels: `agent::coach`, `type::task`) and rendered as native markdown. Each week shows:
- Daily sessions with explicit wattage (HOME Rule #6 compliant — no % FTP)
- Duration, load (TSS), and Intervals.icu event IDs
- Execution tracking checklists

---

## 📖 Documentation

| Document | Audience |
|:---|:---|
| [User Guide](docs/user_guide.md) | @fpittelo — how to use the dashboard |
| [Admin Guide](docs/admin_guide.md) | @devops — deployment, Docker, env vars |
| [Specifications](docs/specifications.md) | @architect — technical specs, data flow, MCP integration |
| [Architecture](docs/architecture.md) | @architect — ArchiMate 3.x model, C4 diagrams, ADRs |

> ℹ️ The `docs/` set is refreshed for the local-first deployment model (ADR-007, issue #108); the local-topology STRIDE rewrite lands with #113.

---

## 🔒 Security & Privacy

- **Swiss nLPD (FADP) Compliant** — all biometric data (HR, HRV, sleep, training loads) stays in ephemeral browser session. No persistent client-side storage.
- **Local-First Deployment (ADR-007)** — dev, qa, and prod run on the local workstation in hardened, loopback-only Docker lanes; biometric data never leaves the host. Loopback binding is the primary access control; Google OIDC (prod lane) is defense-in-depth.
- **No API Key in the Browser** — the Intervals.icu API key lives **only** in the Coach MCP server's environment. The web app never touches it.
- **OCI Non-Root Container** — runs as unprivileged user `coach-web` (`UID:GID 10001:10001`), same hardening as the Coach MCP server.
- **Clean stdout** — structured logging with no secrets logged.

---

## 🧱 Tech Stack

| Layer | Technology |
|:---|:---|
| **Language** | Python 3.12 |
| **Framework** | FastAPI + uvicorn |
| **Frontend** | Alpine.js + hand-written CSS with v0.7 design tokens (static assets, zero CDN) |
| **Streaming** | Server-Sent Events (`sse-starlette`) |
| **MCP Client** | `mcp[cli]` Python SDK (streamable_http transport) |
| **GitHub API** | `httpx` async client (for training plan issues) |
| **Validation** | Pydantic v2 + `pydantic-settings` |
| **Container** | `python:3.12-slim`, multi-stage, non-root UID 10001 |
| **CI/CD** | GitHub Actions: `ruff` + `mypy --strict` + `pytest` + Docker build |

---

## 🌳 Branching & Releases

Coach Web follows the [HOME Governance 3-branch lifecycle](https://github.com/fpittelo/coach):

```mermaid
gitGraph
    commit id: "Init main"
    branch qa
    checkout qa
    commit id: "Init qa"
    branch dev
    checkout dev
    commit id: "Init dev"
    branch feature/59-fastapi-scaffold
    checkout feature/59-fastapi-scaffold
    commit id: "feat: FastAPI scaffold"
    checkout dev
    merge feature/59-fastapi-scaffold id: "PR (Clean CI)"
    checkout qa
    merge dev id: "Promote to QA"
    checkout main
    merge qa id: "Release v0.2.0"
```

- `dev` — active development integration
- `qa` — staging validation
- `main` — production release (`ghcr.io/fpittelo/coach-web:latest`)

---

## 📄 License

**GNU General Public License v3.0 or later (GPL-3.0-or-later)** — see [LICENSE.md](LICENSE.md).

Same license as the Coach MCP server. Open source, copyleft, built with love in Switzerland. 🇨🇭

---

## 🙌 Acknowledgements

- **[Intervals.icu](https://intervals.icu)** — the best endurance analytics platform on the planet
- **[Model Context Protocol](https://modelcontextprotocol.io)** — Anthropic's protocol for LLM-tool integration
- **[FastAPI](https://fastapi.tiangolo.com)** — high-performance async Python web framework
- **[HOME SCRUM Team](https://github.com/fpittelo)** — @architect, @scrum-master, @developer, @devops, @cyber-security, @code-reviewer

---

> _Built by the HOME SCRUM Team for @fpittelo. Because watts don't lie, and neither should your dashboard._
