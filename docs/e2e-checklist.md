# 🧪 E2E Checklists

Two complementary verification tracks:

1. **Agent journey verification (dev/qa lanes)** — autonomous, executed by `@developer`/`@devops` via the `BROWSER` MCP tools (MADR-0009, epic #133). See [Agent Journey Verification](#agent-journey-verification-devqa-lanes) below.
2. **Manual prod-lane release gate** — credential-dependent happy path executed by @fpittelo (below). Agents never operate the prod lane.

---

# 🤖 Agent Journey Verification (dev/qa lanes)

**Owner:** @developer (TDD cycles) / @devops (lane validation)  
**Lane:** dev (`compose.yaml` + `compose.dev.yml`, `127.0.0.1:8100`) or qa (`compose.qa.yml`, `127.0.0.1:8200`)  
**Tooling:** `scripts/lane.sh` (lifecycle) + `BROWSER_*` MCP tools (browsing) — MADR-0009  
**Epic:** #133

## Containment convention (binding for all agents)

- Container operations **only** via `scripts/lane.sh` — compose projects `coach-web-dev` / `coach-web-qa` exclusively; never stop, inspect, or remove unrelated Docker resources on the host.
- Browser navigation **only** to the lane URLs (`http://127.0.0.1:8100`, `http://127.0.0.1:8200`). No external sites, no other localhost services, no other projects' ports.
- **Prod lane is out of agent scope** — manual procedure only (see the prod checklist below).
- These rules are convention-enforced (PO decision, MADR-0009 residual risks) and audited by `@code-reviewer` on every PR whose report shows a violation.

## Lifecycle

```bash
./scripts/lane.sh dev up      # build + start + wait healthy → prints lane URL
./scripts/lane.sh dev status  # service states (lane project only)
./scripts/lane.sh dev logs coach-web
./scripts/lane.sh dev down    # scoped teardown (--remove-orphans)
```

`scripts/e2e-preflight.sh` remains the **validation** gate (it tears down after checking); `lane.sh` is the **lifecycle** tool that keeps a lane running for journey execution.

## Agent-executable journeys (dev lane @ `127.0.0.1:8100`)

Execute with `BROWSER_*` tools: `browser_navigate` → `browser_snapshot` (accessibility tree) → `browser_click` / `browser_type` → `browser_console_messages`. Assert against the **snapshot**, not screenshots.

| # | Journey | Steps | Expected UI state |
|---|---|---|---|
| J1 | Onboarding empty state | Navigate to `/`; snapshot | Hero monogram + welcome line ("Hello! I'm your coach — what should we work on today?") + exactly 3 starter chips |
| J2 | Chip prefill (no auto-send) | Click chip 1; snapshot | Composer contains the chip text and holds focus; **nothing sent**; no new message in the log |
| J3 | Send + stream | Type a short coaching question; press Enter; poll snapshots while streaming | User bubble appears; assistant response streams; phase indicator transitions (waiting → streaming); final message rendered once |
| J4 | Multi-turn follow-up | Send a follow-up referencing the previous answer | Coherent reply referencing conversation context (history contract #79) |
| J5 | Plan proposal + approval card | Ask for a weekly plan; snapshot after proposal | Inline plan card beneath the proposing message with Approve/Reject; exactly one active card |
| J6 | Plan reject | Click Reject; snapshot | Card dismissed; no plan state residue |
| J7 | New chat reset | Click New chat mid- or post-stream; snapshot | Log cleared, empty state returns; no stale stream events resurrect content |
| J8 | XSS sanitization | Send `<script>alert(1)</script>`, `<img src=x onerror=alert(1)>`, `[x](javascript:alert(1))`, and `<scr` + `ipt>` as two consecutive messages (accumulated-string re-sanitization across stream chunks, STRIDE C3) | All inert in assistant bubble (sanitized markdown); user bubble plain text; no execution; no console errors |
| J9 | Console hygiene | After J1–J8, read console messages | No uncaught errors/warnings attributable to the app (browser-extension noise excluded) |

**Pass criterion:** every journey's snapshot assertions hold and J9 is clean. A failed journey blocks sign-off and is filed as `type::bug` with the snapshot excerpt.

## PR-comment report template (post in the validating agent's PR thread)

```markdown
### 🧪 Agent Journey Verification Report

- **Lane:** dev | qa — `http://127.0.0.1:<port>`
- **Image:** `<image>@sha256:<digest>` (from `docker compose images` or the release notes)
- **Journeys:** J1 ✅ · J2 ✅ · J3 ✅ · J4 ✅ · J5 ✅ · J6 ✅ · J7 ✅ · J8 ✅ · J9 ✅
- **Console errors:** none | <list>
- **Failures:** none | <journey # + snapshot excerpt + filed issue>
- **Teardown:** ✅ `lane.sh <lane> down` executed — host clean (verified via `lane.sh status` empty)
- **Verdict:** PASS | FAIL
```

**Redaction rule (MADR-0009, binding):** reports and PR comments must **not** include console/page content containing personal or health data, credentials, or tokens — summarize, redact, or omit. Snapshot excerpts quoted in failure reports are limited to the minimum lines needed to demonstrate the failing assertion.

## DoD hook

The closing DoD verification for any story validated this way gains the item:

- [ ] **Environment cleaned:** test lane torn down (`lane.sh <lane> down`) and confirmed in the journey report.

> Rider: the canonical DoD template in the `github-scrum-board` skill carries the same item once the pending skill revision lands (see epic #133); until then this section is the governing checklist for epic #133 stories.

---

# 🧪 Manual E2E Checklist — prod lane (v0.8.0)

**Owner:** @fpittelo  
**Lane:** prod (`compose.yaml` + `compose.prod.yml`, `127.0.0.1:8000`)  
**Issue:** #113 (release gate for v0.8.0)

> This checklist covers the **credential-dependent happy path** that cannot run in CI:
> **OIDC login → agent chat → tool calls → plan approval**. The automatable parts
> (401 without a session, healthchecks, loopback-only publishing, exact service-set
> presence) are covered by `tests/` and `scripts/e2e-preflight.sh`.

## Prerequisites

- [ ] `.env.prod` exists, is `chmod 600`, and carries: `COACH_WEB_IMAGE` (digest-pinned), `OPENROUTER_API_KEY`, `GITHUB_TOKEN` (read+write to the plan branch), `INTERVALS_API_KEY`, `GOOGLE_OIDC_CLIENT_ID`, `GOOGLE_OIDC_CLIENT_SECRET`, a lane-distinct `AUTH_SESSION_SECRET` (≥32 bytes), `AUTH_WHITELIST_EMAILS`.
- [ ] Google OAuth client registers redirect URI `http://localhost:8000/auth/callback` **and** `http://127.0.0.1:8000/auth/callback`.
- [ ] `docker login ghcr.io` with a `read:packages` PAT.
- [ ] Use a browser with the loopback `Secure`-cookie exception (Chromium/Firefox). **Safari will not store the session cookie over plain HTTP.**

## Step 0 — Bring up the lane and run the automated gate

```bash
docker compose -p coach-web-prod -f compose.yaml -f compose.prod.yml --env-file .env.prod pull
docker compose -p coach-web-prod -f compose.yaml -f compose.prod.yml --env-file .env.prod up -d
./scripts/e2e-preflight.sh prod
```

- [ ] Pre-flight exits `0` and reports all three services present and healthy.
- [ ] `docker compose -p coach-web-prod ps -a` shows `coach-web`, `coach-mcp`, `github-mcp` all `healthy`.

## Step 1 — Anonymous access is denied (auth boundary)

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' \
  -d '{"message":"probe"}' http://127.0.0.1:8000/api/agent/stream
```

- [ ] Returns **401** (no session). The stream endpoint is POST-only (#79).
- [ ] `curl -sS -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example' http://127.0.0.1:8000/` returns **400** (TrustedHostMiddleware, DNS-rebinding mitigation).

## Step 2 — OIDC login

1. Open `http://localhost:8000/` in the browser.
2. Click **Sign in** → redirected to Google.
3. Authenticate as the whitelisted account (`AUTH_WHITELIST_EMAILS`).

- [ ] Redirected back to `http://localhost:8000/` with a session cookie (`cw_session`) set.
- [ ] DevTools → Application → Cookies shows `cw_session` with `HttpOnly`, `Secure`, `SameSite=Lax`.
- [ ] A non-whitelisted Google account is rejected (403), not admitted.

## Step 3 — Agent chat

1. Open the coach chat panel.
2. Ask a coaching question (e.g. *“How is my training load trending this week?”*).

- [ ] The agent streams a response (SSE) without error.
- [ ] The response references real Intervals.icu metrics (proves the `coach-mcp` tool call succeeded).

## Step 4 — Tool calls

- [ ] The agent invokes `coach-mcp` (Intervals.icu metrics) and/or `github-mcp` (plan issues) — visible in the streamed tool activity.
- [ ] No secret material (API keys, tokens) appears in the UI, network responses, or container logs:
  ```bash
  docker compose -p coach-web-prod logs --tail 200 | grep -iE 'sk-or-|ghp_|api[_-]?key' || echo 'no secrets in logs'
  ```

## Step 5 — Plan approval

1. Ask the agent to draft a weekly plan.
2. Review the plan approval card and click **Approve**.

- [ ] `POST /api/plan/approve` returns success.
- [ ] The plan Markdown is committed to `fpittelo/coach` (verify the commit in GitHub).
- [ ] The workouts appear on the Intervals.icu calendar.
- [ ] The approval is attributable to the owner (single whitelisted identity).

## Step 6 — Teardown

```bash
docker compose -p coach-web-prod -f compose.yaml -f compose.prod.yml --env-file .env.prod down
```

- [ ] Lane torn down; no orphan containers (`docker compose -p coach-web-prod ps -a` is empty).

## Sign-off

| Field | Value |
|:---|:---|
| Verified by | @fpittelo |
| Date | |
| Result | ☐ pass ☐ fail |
| Notes | |

> Record the sign-off in the v0.8.0 promotion PR. A failed step blocks the release gate (see `docs/admin_guide.md` § Release Gate).
