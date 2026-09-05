# 📕 Architecture — Coach Web

**Audience:** @architect, @devops  
**Last updated:** 2026-09-05

---

## 1. ArchiMate 3.x Metamodel

### Motivation Layer

```mermaid
graph LR
    DRIVER["Driver: Interactive Athletic Dashboard"]
    GOAL["Goal: Visualize Intervals.icu data"]
    REQ["Requirement: Streamlit Web App"]
    PRINCIPLE["Principle: KIS & Python-Native"]
    CONSTRAINT["Constraint: Swiss nLPD / FADP"]

    DRIVER -->|"Influences"| GOAL
    CONSTRAINT -->|"Influences"| GOAL
    GOAL -->|"Realizes"| PRINCIPLE
    PRINCIPLE -->|"Realizes"| REQ
```

### Business Layer

```mermaid
graph LR
    ACTOR["Business Actor: @fpittelo"]
    PROCESS["Business Process: Sprint Delivery"]
    CAPABILITY["Capability: Endurance Cockpit"]
    VALUE["Value Stream: Train → Recover → Adapt"]

    ACTOR -->|"Performs"| PROCESS
    CAPABILITY -->|"Realizes"| VALUE
```

### Application Layer

```mermaid
graph TD
    subgraph "Application Components"
        APP["Streamlit App<br/>coach_web.app"]
        DASH["Dashboard Component<br/>coach_web.dashboard"]
        PLANS["Plans Viewer<br/>coach_web.plans"]
        CHAT["Chat Interface<br/>coach_web.chat"]
        MCP_CLIENT["MCP Client<br/>coach_web.mcp_client"]
        GH_CLIENT["GitHub Client<br/>coach_web.github_client"]
        CONFIG["Config<br/>coach_web.config"]
        MODELS["Models<br/>coach_web.models"]
    end

    APP --> DASH
    APP --> PLANS
    APP --> CHAT
    DASH --> MCP_CLIENT
    PLANS --> GH_CLIENT
    CHAT --> MCP_CLIENT
    MCP_CLIENT --> CONFIG
    GH_CLIENT --> CONFIG
    DASH --> MODELS
    PLANS --> MODELS
```

### Technology Layer

```mermaid
graph TD
    NODE["Node: Docker Container<br/>python:3.12-slim"]
    SYS["System Software: Python 3.12, Streamlit"]
    CI["CI/CD: GitHub Actions"]
    REGISTRY["Registry: GHCR<br/>ghcr.io/fpittelo/coach-web"]

    SYS -->|"Realizes"| NODE
    CI -->|"Builds → Pushes"| REGISTRY
    NODE -->|"Hosts"| APP["Application: coach_web"]
```

---

## 2. C4 Model — System Context

```mermaid
graph TD
    subgraph "Person"
        USER(["@fpittelo<br/>Athlete"])
    end

    subgraph "coach-web [System]"
        APP["Coach Web<br/>Streamlit Dashboard"]
    end

    subgraph "coach [System — External]"
        MCP["Coach MCP Server<br/>FastMCP"]
    end

    subgraph "External Systems"
        GITHUB["GitHub<br/>Issues API"]
        INTERVALS["Intervals.icu<br/>REST API"]
    end

    USER -->|"Views dashboard, plans, chats"| APP
    APP -->|"MCP streamable_http / SSE"| MCP
    APP -->|"REST API + token"| GITHUB
    MCP -->|"HTTPS + Basic Auth"| INTERVALS
```

---

## 3. C4 Model — Container View

```mermaid
graph TD
    subgraph "coach-web Container"
        APP["Streamlit App<br/>:8501<br/>Python 3.12"]
        CACHE["@st.cache_data<br/>TTL: 60-300s"]
    end

    subgraph "coach Container [External]"
        MCP["FastMCP Server<br/>:8000<br/>streamable_http"]
        MCP_CACHE["TTL Cache<br/>write-through"]
    end

    subgraph "External"
        GITHUB["GitHub REST API"]
        INTERVALS["Intervals.icu REST API"]
    end

    APP --> CACHE
    APP -->|"MCP over HTTP/SSE"| MCP
    MCP --> MCP_CACHE
    MCP -->|"HTTPS"| INTERVALS
    APP -->|"HTTPS + Bearer"| GITHUB
```

---

## 4. Deployment Topology

```mermaid
graph TD
    subgraph "Docker Host"
        WEB_CONTAINER["coach-web Container<br/>UID 10001<br/>:8501"]
        MCP_CONTAINER["coach Container<br/>UID 10001<br/>:8000"]
    end

    subgraph "External Network"
        INTERVALS["Intervals.icu"]
        GITHUB["GitHub"]
        GHCR["ghcr.io"]
    end

    WEB_CONTAINER -->|"HTTP :8000/mcp"| MCP_CONTAINER
    MCP_CONTAINER -->|"HTTPS"| INTERVALS
    WEB_CONTAINER -->|"HTTPS"| GITHUB
    GHCR -->|"docker pull"| WEB_CONTAINER
    GHCR -->|"docker pull"| MCP_CONTAINER
```

---

## 5. ADR-001: Repository Separation

## ADR-001: Separate Repository for Coach Web

| Field | Value |
|:---|:---|
| **Status** | Accepted |
| **Date** | 2026-09-05 |
| **Deciders** | @architect, @scrum-master, @developer, @devops, @cyber-security |
| **Reviewed by** | @fpittelo |

### Context

The product owner (@fpittelo) requested a modern web application to interactively visualize Intervals.icu biometric data and display weekly training plans. The existing `fpittelo/coach` repository is a Python FastMCP server with a mature CI/CD pipeline (311 tests, 91% coverage, `ruff` + `mypy --strict` + `pytest` + Docker).

### Decision

**Create a separate repository `fpittelo/coach-web` for the web application.**

### Rationale

1. **Protocol Decoupling (@architect):** The web app is a *client* of the Coach MCP server, communicating over the MCP `streamable_http` transport. They share no runtime code. Repository boundaries mirror this runtime boundary.
2. **Sprint Independence (@scrum-master):** Frontend sprints (UI, UX, charts) have a different backlog shape than backend sprints (API tools, Intervals.icu integration). Separate repos preserve clean milestone tracking and velocity measurement.
3. **Tech Stack Divergence (@developer):** While both repos are Python, the web app uses Streamlit + Plotly + MCP client SDK, distinct from FastMCP + Pydantic + httpx server modules. Separate `pyproject.toml` files keep dependency trees clean.
4. **CI/CD Independence (@devops):** Separate GitHub Actions workflows, separate GHCR image registries (`ghcr.io/fpittelo/coach` vs `ghcr.io/fpittelo/coach-web`), independent release cadences. A frontend hotfix should not trigger a backend image rebuild.
5. **Security Boundary (@cyber-security):** The browser is an untrusted runtime for biometric data. Separate repos enforce a hard boundary between the hardened MCP backend (holds API key) and the web client (never touches the API key). Swiss nLPD/FADP data minimization is easier to enforce per-repo.
6. **KIS Principle (@fpittelo):** Apply Keep It Simple — one repo per deployable artifact, one language per repo, one CI pipeline per repo.

### Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|:---|:---|:---|:---|
| **Monorepo** | Shared CI, one clone | Mixed toolchain, coupled releases, ambiguous rollback | ❌ Rejected |
| **Separate repo (chosen)** | Clean boundaries, independent releases, focused CI | Slight overhead (two repos) | ✅ Accepted |

### Consequences

- `coach` repo stays focused on MCP server development
- `coach-web` repo handles all frontend concerns
- Both repos independently follow the HOME 3-branch lifecycle (`dev` → `qa` → `main`)
- Both repos use the same Python 3.12 + `uv` + `ruff` + `mypy` + `pytest` quality gates
- Communication between repos is via the MCP protocol contract, not shared source code

---

## 6. ADR-002: Streamlit Framework Choice

| Field | Value |
|:---|:---|
|:---|:---|
| **Status** | Accepted |
| **Date** | 2026-09-05 |
| **Deciders** | @architect, @developer, @fpittelo |

### Context

The web app needs to display interactive charts (Plotly), render markdown training plans, and provide a chat interface — all in Python.

### Decision

**Use Streamlit as the web framework.**

### Rationale
1. **KIS:** `streamlit run app.py` — one file, zero config, no frontend toolchain
2. **Python-native:** Stays in `uv`/`pip` ecosystem — no npm, no TypeScript
3. **Built-in charts:** `st.plotly_chart()` — no separate charting setup
4. **Markdown rendering:** `st.markdown()` renders training plan issues natively
5. **Chat UI:** `st.chat_message` + `st.chat_input` — built-in conversational interface
6. **Modern look:** Clean default UI, not a legacy admin panel
7. **MCP integration:** Compatible with the `mcp[cli]` Python SDK over `streamable_http`

### Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|:---|:---|:---|:---|
| **NiceGUI** | Modern look, FastAPI-based | Quasar/Vue complexity under the hood | ❌ Rejected |
| **FastAPI + Jinja2 + HTMX** | More control | More boilerplate, manual UI polish | ❌ Rejected |
| **Gradio** | ML-focused | Optimized for ML demos, not dashboards | ❌ Rejected |
| **Streamlit (chosen)** | KIS, Python-native, built-in charts + chat | Limited customization (acceptable) | ✅ Accepted |

---

## 7. Repository Structure

```
fpittelo/coach-web/
├── .github/
│   └── workflows/
│       ├── ci.yaml           # Lint + test + Docker build validation
│       └── deploy.yaml       # GHCR image push (dev/qa/prod)
├── src/
│   └── coach_web/
│       ├── __init__.py
│       ├── app.py            # Streamlit entry point
│       ├── config.py         # Pydantic Settings
│       ├── mcp_client.py     # MCP SDK client wrapper
│       ├── github_client.py  # GitHub API client
│       ├── dashboard.py      # Readiness dashboard
│       ├── plans.py          # Weekly plan viewer
│       ├── chat.py           # Coach chat interface
│       └── models.py         # Pydantic v2 models
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_config.py
│   ├── test_mcp_client.py
│   ├── test_github_client.py
│   ├── test_dashboard.py
│   ├── test_plans.py
│   └── test_chat.py
├── docs/
│   ├── user_guide.md         # End-user guide
│   ├── admin_guide.md        # Deployment & CI/CD
│   ├── specifications.md     # Technical specs
│   ├── architecture.md       # This file (ArchiMate + C4 + ADRs)
│   └── security.md           # Privacy & STRIDE
├── .env.example
├── .dockerignore
├── .gitignore
├── Dockerfile                # Multi-stage, non-root UID 10001
├── pyproject.toml            # Build + deps + tool config
├── LICENSE.md                # GPL-3.0-or-later
└── README.md
```

---

## 8. Cross-Repository Dependencies

```mermaid
graph LR
    subgraph "coach-web repo"
        WEB["coach_web app"]
    end

    subgraph "coach repo [External]"
        MCP["Coach MCP Server"]
        TOOLS["MCP Tools:<br/>intervals_get_readiness_dashboard<br/>intervals_get_athlete_profile<br/>intervals_get_fitness_summary"]
    end

    subgraph "Contract"
        PROTOCOL["MCP Protocol<br/>streamable_http"]
    end

    WEB -->|"consumes"| PROTOCOL
    PROTOCOL -->|"exposes"| MCP
    MCP --> TOOLS
```

**Contract:** The MCP protocol over `streamable_http` is the integration contract. The `coach-web` repo depends on the Coach MCP server's *published tool schema*, not its source code.

---

_Last updated: 2026-09-05_