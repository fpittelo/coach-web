# 📕 Architecture — Coach Web

**Audience:** @architect, @devops  
**Last updated:** 2026-10-03

> **Authority note:** Sections 1–3 and 7–8 still describe the retired Streamlit architecture (ADR-002) and are slated for a full refresh. **ADR-006 (§9)** is authoritative for the visual/rendering contract; **ADR-007 (§10)** is authoritative for the deployment topology (§4).

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
        INTERVALS["Intervals.icu<br/>REST API"]
    end

    APP --> CACHE
    APP -->|"MCP over HTTP/SSE"| MCP
    MCP --> MCP_CACHE
    MCP -->|"HTTPS"| INTERVALS
    APP -->|"HTTPS + Bearer"| GITHUB
```

---

## 4. Deployment Topology (ADR-007 — Local-First)

> **Authoritative deployment model: ADR-007 (§10).** The local Docker host on the workstation is the deployment target for all three lanes; the GCP Cloud Run topology (Sprint 08, #64–#68) is retired and decommissioned via #111.

```mermaid
graph TD
    subgraph "Local Docker Host — workstation (loopback 127.0.0.1 only)"
        subgraph "Lane (dev :8100 / qa :8200 / prod :8000)"
            WEB["coach-web Container<br/>UID 10001 · FastAPI/uvicorn"]
            MCP["coach-mcp :8000 (SSE)<br/>UID 10001 · holds INTERVALS_API_KEY"]
            GHMCP["github-mcp :8001<br/>holds GITHUB PAT"]
        end
    end

    subgraph "External"
        GHCR["ghcr.io"]
        INTERVALS["Intervals.icu"]
        GITHUB["GitHub"]
    end

    OWNER(["@fpittelo<br/>browser, loopback only"]) -->|"127.0.0.1:{lane-port}"| WEB
    WEB -->|"http://coach-mcp:8000/sse"| MCP
    WEB -->|"http://github-mcp:8001/"| GHMCP
    MCP -->|"HTTPS"| INTERVALS
    GHMCP -->|"HTTPS"| GITHUB
    GHCR -->|"docker pull (qa/prod lanes)"| WEB
    GHCR -->|"docker pull"| MCP
```

### Lane Matrix

| Lane | Compose files | coach-web image | Host port | Auth | Purpose |
|:---|:---|:---|:---|:---|:---|
| **dev** | `compose.yaml` + `compose.dev.yml` | build from source (`target: runtime`) | `127.0.0.1:8100` | `AUTH_ENABLED=false` | active development |
| **qa** | `compose.yaml` + `compose.qa.yml` | `ghcr.io/fpittelo/coach-web:qa` | `127.0.0.1:8200` | `AUTH_ENABLED=false` | staging validation of promoted `dev` |
| **prod** | `compose.yaml` + `compose.prod.yml` | digest-pinned `vX.Y.Z` | `127.0.0.1:8000` | `AUTH_ENABLED=true` (OIDC defense-in-depth) | daily-use deployment |

**Properties (all lanes):** per-lane compose project name (`coach-web-dev|qa|prod`) and network; sidecars publish no host ports; hardening parity (`read_only`, `cap_drop: ALL`, `no-new-privileges`, tmpfs, resource limits, non-root UID 10001). Promotion stays PR-approval-gated (`dev` → `qa` → `main`); the local lane pull is the procedural rollout step. Lane files are delivered by #109.

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
| **Status** | Accepted (superseded in practice by the v0.5 FastAPI migration — see ADR-006 note) |
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

## 9. ADR-006: v0.7 Visual Contract Supersession

| Field | Value |
|:---|:---|
| **Status** | Accepted |
| **Date** | 2026-09-19 |
| **Deciders** | @architect, @developer, @scrum-master, @cyber-security |
| **Reviewed by** | @fpittelo |
| **Supersedes** | Sprint 03 Swiss minimalist visual contract (test-enforced, undocumented) |
| **Tracked in** | Epic #88 — Sprint 09 (v0.7.0), Milestone 9 |

### Context

Sprint 03 (v0.3.0) established a Swiss minimalist visual contract: a strict token set (`#111` ink, `#fff` paper, `#ff0000` accent, `--radius: 2px`, 1px hairline borders, no shadows, emoji-free Inter typography), enforced by contract tests in `tests/test_static_assets.py` (`test_stylesheet_uses_swiss_tokens` asserts the absence of shadows and large radii; `test_index_uses_text_interpolation_only` forbids `x-html` entirely).

The v0.7 UI/UX Overhaul epic (Sprint 09, #88 — groomed 2026-09-19 through a code-grounded technical grilling by @developer, 12 product decisions signed off by @fpittelo, and a KIS/YAGNI scope pass) requires a client-ready conversational experience that is incompatible with several clauses of that contract: softer radii and shadows, a slate/teal palette, a centered 760px single column, and sanitized markdown rendering of assistant messages (an intentional, gated reversal of the x-text-only posture).

### Decision

**Supersede the Sprint 03 visual contract with the v0.7 conversational design contract:**

1. **Design tokens:** off-white surfaces (`#F8FAFC`/`#FFFFFF`), ink `#1E293B`, muted ink `#475569`, hairline `#E2E8F0`, accent deep teal `#0F766E`, accent ink `#FFFFFF`; `--radius: 10px` (band 8–12px); soft shadow tokens replace harsh black hairline borders. Single centered column, `max-width: 760px`. Inter remains the sole typeface (self-hosted WOFF2, unchanged since Sprint 03).
2. **Deliberate test supersession:** `test_stylesheet_uses_swiss_tokens` is rewritten to assert the v0.7 token contract instead of forbidding shadows/radii. No test is deleted without a replacement assertion (zero-warning gate unchanged).
3. **Rendering posture (assistant-only):** assistant messages render `DOMPurify.sanitize(marked.parse(text))` via `x-html` — vendored, self-hosted, zero CDN. User messages remain `x-text` forever. `test_index_uses_text_interpolation_only` is rewritten to allow `x-html` only on assistant messages via the sanitizer helper. **This reversal is security-gated:** STRIDE review #87 (@cyber-security) must sign off before #81 merges.
4. **Transport & conversation:** `GET /api/agent/stream` is replaced by `POST /api/agent/stream` (Pydantic v2 body `{message, history}`; history capped at 10 entries; roles `user`/`assistant` only) — client-owned history, no server session store (nLPD ephemeral posture), and message content no longer leaks into access-log query strings (#79).
5. **Styling strategy:** hand-written CSS + design tokens. No Tailwind, no Node toolchain, no icon library (single inline SVG monogram). The KIS/YAGNI cut list and locked product decisions are recorded in epic #88.

### Rationale

1. **Client-ready quality bar (PO Q1):** the v0.3 contract reads as an internal prototype; the epic's conversational ergonomics (bubbles, inline plan cards, pinned composer, onboarding chips) require softer geometry and a focused reading column.
2. **Accessibility:** slate-on-off-white with a deep teal accent meets WCAG AA contrast; the 8–12px radius band + soft shadows is the epic's explicit visual direction (PO Q5).
3. **Security by gate, not by avoidance:** forbidding `x-html` entirely was the simplest v0.5 posture; the proportionate v0.7 posture is sanitization + STRIDE review + contract tests — enabling markdown value without exposing the browser to untrusted HTML (user input never renders as HTML; the plan card stays structured Pydantic→HTML).
4. **Privacy by design (Swiss nLPD):** POST transport removes message text from URLs; client-owned history keeps the server stateless and the browser session ephemeral.
5. **KIS/YAGNI:** only two new vendored assets (marked, DOMPurify); no frameworks, no build toolchain, no icon set; deliberate non-goals are recorded in epic #88.

### Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|:---|:---|:---|:---|
| **Keep v0.3 contract, style around it** | Zero test churn | Blocks the epic's core visual goals; compliance theatre | ❌ Rejected |
| **Adopt Tailwind (vendored/CDN)** | Utility speed | Requires Node build or multi-KB CDN payload; contradicts zero-CDN and hand-written CSS; README claim already wrong | ❌ Rejected |
| **Server-side markdown rendering** | One render path | Template pipeline rework; plan approval needs raw JSON anyway; client-side sanitization is proportionate | ❌ Rejected |
| **v0.7 tokens + gated x-html (chosen)** | Epic goals met; security-gated; minimal asset footprint | Requires deliberate test rewrites (this ADR) and STRIDE review | ✅ Accepted |

### Consequences

- `tests/test_static_assets.py` contract tests are rewritten per this ADR in #78 and #81 — test supersession is explicit, reviewed, and logged; never silent deletion.
- #81 is merge-blocked on STRIDE sign-off (#87): sanitizer configuration, mid-stream partial renders, CSP posture (including the Alpine `unsafe-eval` question), and replayed-history prompt injection.
- `README.md` tech-stack table is corrected in #78: the repo uses hand-written CSS, not Tailwind.
- Sections 1–3 and 7–8 of this document still describe the retired Streamlit architecture (ADR-002) and are slated for a full refresh (§4 was refreshed by ADR-007, 2026-10); **ADR-006 is authoritative for the visual/rendering contract going forward.**
- The v0.3 Inter typography decision (self-hosted WOFF2, zero CDN) carries forward unchanged.

---

## 10. ADR-007: Local-First Deployment

| Field | Value |
|:---|:---|
| **Status** | Accepted |
| **Date** | 2026-10-03 |
| **Deciders** | @fpittelo (Product Owner decision), @architect, @scrum-master, @devops, @cyber-security |
| **Reviewed by** | @fpittelo |
| **Supersedes** | Sprint 08 Phase 2 serverless migration (#64–#68: OpenTofu GCP IaC, multi-container Cloud Run, WIF keyless CI; #68 closed `not_planned`) |
| **Tracked in** | Epic #107 — Sprint 10 (v0.8.0), Milestone 10 |

### Context

Sprint 08 (Phase 2) delivered a complete serverless GCP topology into `dev`: OpenTofu IaC pinned to `europe-west6` (#64), Google OIDC authentication with an application-level email whitelist (#65), a multi-container Cloud Run service (#66), and keyless Workload Identity Federation CI (#67). Issue #68 (live Cloud Run E2E validation & zero-scale audit) was in progress when the Product Owner re-evaluated the operating model.

On 2026-10-03, @fpittelo decided to **abandon the GCP cloud deployment entirely** and run the stack on the local workstation (VIDAR) via Docker for all three lanes (dev, qa, prod). #68 was closed `not_planned` (superseded by #113 — local E2E validation & release gate); the already-merged OIDC implementation (#65, PR #99) carries forward unchanged.

### Decision

**Local Docker on the workstation becomes the deployment target for dev, qa, and prod.**

1. **Lanes:** base `compose.yaml` + per-lane override files (`compose.dev.yml`, `compose.qa.yml`, `compose.prod.yml`) with per-lane compose project names and networks, so lanes run concurrently without collisions. All host bindings are `127.0.0.1` only — **loopback binding is the primary access control**. Compose *profiles* were rejected: they select which services run, not variants of the same service.
2. **Promotion artifact:** GHCR lane-tag images (`:dev`, `:qa`, `:prod`/`vX.Y.Z`) remain the promotion artifact — the qa and prod lanes pull CI-built, zero-warning-gated images rather than building locally; the prod lane is digest-pinned. Promotion `dev` → `qa` → `main` stays PR-approval-gated by @fpittelo; the local lane pull is the procedural rollout step.
3. **Auth:** loopback binding is the primary boundary; the Google OIDC email whitelist (#65) is **defense-in-depth**, enabled on the prod lane (`AUTH_ENABLED=true`, redirect `http://localhost:8000/auth/callback`); dev/qa run `AUTH_ENABLED=false`.
4. **Cloud retirement:** `infra/` OpenTofu IaC, the `deploy-gcp` CI stage, the GCP repo variables, and the `production` GitHub environment are removed (#110 — in the same PR as the `ci.yaml` `opentofu` job, or CI breaks); all GCP resources are decommissioned with explicit @fpittelo approval and captured evidence (#111).
5. **Secrets:** per-lane, per-service env files replace the shared `.env` — each container receives only the secrets it needs, with distinct `AUTH_SESSION_SECRET` per lane (#112).

### Rationale

1. **KIS (@fpittelo):** one Docker host, one compose topology, zero cloud consoles. The local sidecar topology already existed and was proven (issue #63, `e2e-preflight.sh`).
2. **Cost:** $0 fixed cost, unconditionally — no dependence on scale-to-zero behavior or idle-timeout tuning.
3. **Swiss nLPD/FADP (@cyber-security):** biometric data storage and processing stay on the workstation — a residency improvement over `europe-west6`. Residual risk: the OpenRouter LLM call remains a cross-border *processing* transfer of biometric context (assessed in #112).
4. **Security posture:** loopback-only binding removes the public edge entirely; the hardened container posture (non-root, `read_only`, `cap_drop: ALL`, `no-new-privileges`) is unchanged.
5. **Salvage:** the OIDC implementation (#65) and hardened GHCR images carry forward unchanged — only the platform target changes.

### Alternatives Considered

| Alternative | Pros | Cons | Verdict |
|:---|:---|:---|:---|
| **Keep GCP Cloud Run (status quo)** | Already built; remote access; scale-to-zero | Console overhead; standing attention cost; data leaves the workstation; #68 audit still pending | ❌ Rejected (PO decision) |
| **Local build per lane (drop GHCR)** | No registry dependency | qa/prod would run locally-built images bypassing the CI zero-warning gate; base-image drift; no rollback artifact | ❌ Rejected |
| **Compose profiles for lanes** | Single file | Profiles select *services*, not service variants; cannot express per-lane images/ports; `container_name` collisions block concurrent lanes | ❌ Rejected |
| **Base + per-lane override files (chosen)** | Concurrent lanes; CI-gated images; minimal delta from today's compose | Slightly more files; per-lane env discipline required | ✅ Accepted |

### Consequences

- `docs/security.md` access-control framing changes: **loopback binding is primary, the OIDC whitelist is defense-in-depth** (no residual "edge is the boundary" language); the cloud STRIDE model is rewritten for the local topology in #113.
- `docs/admin_guide.md` documents per-lane operations, GHCR pull prerequisites, and the promotion runbook.
- The `deploy-gcp` stage and the `ci.yaml` `opentofu` job are removed in #110; GCP resources are destroyed in #111 (state backups captured first, bootstrap bucket last).
- The v0.7.0 `qa → main` promotion is **held** until #109 + #110 merge — `main` must not ship a deploy stage targeting infrastructure slated for destruction.
- Version order follows release order: if Sprint 10 completes first, local-first releases as v0.7.0 and the v0.7 UI epic becomes v0.8.0 (decided at release time).
- Local trust assumptions accepted and documented: Docker socket / `docker` group is root-equivalent; workstation full-disk encryption and access control are part of the nLPD posture (#112, #113).

---

_Last updated: 2026-10-03_
