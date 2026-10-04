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
