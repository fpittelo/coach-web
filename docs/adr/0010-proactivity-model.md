# MADR-0010: Proactivity Model — Session-Greeting Pull + Passive Pending-Debrief Banner

- **Status:** Accepted (2026-10-10 — per epic #161 grooming decision and PO decisions recorded in fpittelo/coach-web#163, 2026-10-10)
- **Date:** 2026-10-10
- **Deciders:** @architect, @fpittelo (PO approval: grooming session-greeting pattern + PO decisions 1–5, recorded in #163, 2026-10-10)

> **Numbering note:** MADR-0008 (docs/adr/0008-persistence-data-classification.md) is the first standalone file under `docs/adr/`; MADR-0001–0007 remain sections of `docs/architecture.md`. MADR-0009 (agent-journey verification) lives as the MADR appendix of `docs/e2e-checklist.md`. This record takes the next free number, **0010**. The madr-adr lint reconciliation of the sequence gap is tracked as a docs follow-up (see MADR-0008, open question 4).

## Context

Epic #161 (End-to-End Coaching Lifecycle Management, v0.9) Story 3.x requires the coach to **initiate** debrief conversations: after a completed training session, the coach should proactively open the debrief loop rather than waiting for the athlete to ask. Today Coach Web is purely reactive — it responds only to user messages; it never speaks first.

The conversational surface is a **pull-based chat UI** (server-rendered templates + Alpine.js, SSE streaming, ADR-006): the application only runs when the athlete opens the page in a browser lane on the local workstation (ADR-007 local-first, loopback Docker lanes, n=1 single user). There is no mobile app, no always-on server reachable from outside the workstation, and no audience beyond the single whitelisted athlete.

The completed-session source is Intervals.icu (SSOT), accessed on demand through the `coach-mcp` sidecar via the planned `icu_get_completed_sessions` tool (fpittelo/coach#82 — summary fields only, no health values). Dismissal persistence is provided by MADR-0008's `debrief_dismissal_flags` table (reason bounded to the enum `done` | `not-today` | `injury` per security review F10).

This spike (#164) blocks the Epic-3 greeting story of epic #161. Implementation is out of scope — this record only fixes the decision.

## Decision Drivers

- **Pull-based UI reality:** Coach Web executes only in a browser tab the athlete opens; anything that requires the app to act while the page is closed is architecturally impossible without new infrastructure.
- **n=1 local-first (ADR-007):** single user, loopback topology, KIS — zero operational burden is a core architectural value; no scheduler service, cron container, push gateway, or mail relay may be added for a notification feature.
- **No-nagging coaching etiquette:** a debrief prompt repeated after refusal is bad coaching UX; dismissal must be sticky (idempotent) and un-answered sessions must decay into passive artifacts.
- **Rate budget (PO decision 5a, #163):** ≤4 Intervals.icu GETs per user action + server-side cache; capped 429 retries — the greeting check runs on every page load and must not burn the 5000/day ICU budget.
- **Privacy minimization (Swiss nLPD posture, MADR-0008):** the greeting renders before any user action; it must not carry health/telemetry values into the first-paint surface.
- **Latency contract (ADR-006):** first paint and bootstrap are not to be delayed by external API calls; assistant content streams in via SSE after bootstrap.

## Considered Options

#### Option P1: Session-greeting pull on page load (chosen)

On page load, Coach Web asynchronously queries Intervals.icu (via coach-mcp `icu_get_completed_sessions`, bounded ≤90 days per the coach#82 binding conditions) for completed-but-not-debriefed sessions since the last debrief. If any exist and none is dismissed/answered, the **coach's first message in the conversation is the debrief prompt**, streamed in after bootstrap. A **passive, dismissible, non-blocking banner** above the composer mirrors the pending state (UX contract: #165 component 4). Pure pull — no scheduler, no push channel, no new service.

- Pros: zero new infrastructure; works within the pull-based UI the app already has; idempotent via MADR-0008 dismissal flags; greeting content is generated server-side from workout metadata only; naturally rate-budgetable (one cached GET per page load).
- Cons: proactive only when the athlete opens the page (no offline reach) — accepted: at n=1, the athlete is the only person who would ever read the message, and they read it in this UI by definition.

#### Option P2: Server-side scheduler / cron (rejected)

A cron job or background task inside the lane periodically checks ICU and queues debrief prompts.

- Cons: requires a scheduling mechanism in a hardened (`read_only`, non-root, no-new-privileges) container or a new sidecar — new operational surface for zero benefit, since the message cannot be *delivered* anywhere while the page is closed; prompts would pile up unobserved; violates the KIS driver. Rejected at grooming (#163).

#### Option P3: Web Push notifications (rejected)

Service-worker Web Push to reach the athlete when the tab is closed.

- Cons: needs a push subscription endpoint, VAPID key management, service worker, and per-browser permission handling — a notification stack for a single-user loopback app; also conflicts with the no-build-step, server-rendered frontend convention (ADR-006). Infrastructure unjustified at n=1. Rejected at grooming (#163).

#### Option P4: Email / mobile notifications (rejected)

Email or native mobile push as the proactive channel.

- Cons: health-adjacent coaching prompts would leave the workstation boundary by email (nLPD minimization concern) or require a mobile client that does not exist; adds an external dependency and credential surface. Rejected at grooming (#163).

## Decision Outcome

Chosen option: **P1 — session-greeting pull + passive pending-debrief banner**, because it is the only option compatible with the pull-based chat UI and the KIS/local-first drivers, it keeps the proactive surface read-only, metadata-only, and rate-budgeted, and its state is made idempotent by the MADR-0008 persistence layer.

### Greeting state machine

Each completed-but-not-debriefed session moves through:

```
pending → prompted → answered | dismissed → folded-into-weekly-review
```

- **`pending`** — session completed on ICU, not yet surfaced to the athlete.
- **`prompted`** — the coach's greeting has been shown (first message = debrief prompt; banner visible above the composer, #165 component 4).
- **`answered`** — the athlete replies with the debrief (terminal; feeds Story 3.1/3.2).
- **`dismissed`** — the athlete dismisses via the banner; a `debrief_dismissal_flags` row is written per MADR-008 (reason ∈ `done` | `not-today` | `injury`, F10).
- **`folded-into-weekly-review`** — terminal decay state (see staleness rule below).

**Staleness rule (no nagging):** the coach prompts for a `pending`/`prompted` session **the same day it is detected and the next morning only**. If the session is still unanswered after that, it is **folded silently into the weekly review** (weekly_reviews, MADR-0008 C4) — no further greeting, no banner, no re-prompt. A dismissed session is never re-greeted.

### Idempotency

Dismissal flags persist per MADR-008 (`debrief_dismissal_flags`): once a session is dismissed or answered, the page-load check excludes it — **no re-greet for dismissed or answered sessions** on any subsequent page load or lane restart. The greeting check is a pure function of (ICU completed sessions, debrief entries, dismissal flags); re-running it is side-effect-free except for the idempotent `pending → prompted` transition.

### Latency budget

The page-load greeting check is **asynchronous and must not delay first paint**: the UI renders bootstrap immediately; the ICU query runs in the background; the greeting (coach's first message) and the banner **stream in after bootstrap** via the existing SSE/event path (ADR-006 streaming contract). A slow or failing ICU call degrades to "no greeting", never to a slow page.

### Rate budget (PO decision 5a + #163 compensating controls)

- **≤4 ICU GETs per user action** — the greeting check spends at most **1** of the 4 (`icu_get_completed_sessions`).
- **Server-side cache** — the completed-sessions result is cached per lane with a short TTL so repeated page loads / SSE reconnects do not multiply ICU calls.
- **Capped 429 retries** — on Intervals.icu rate-limit responses, retries are bounded (no unbounded backoff that could exhaust the 5000/day ICU budget); exhaustion degrades to "no greeting" for that page load.

### Privacy

The greeting content derives from **workout metadata only — title, time (date/duration), and session type**. No telemetry or health values (power, HR, pace, RPE, wellness scores) appear in the greeting or the banner. Health context enters the conversation only after the athlete engages (debrief flow, Story 3.1), under the existing nLPD posture of MADR-0008 and docs/security.md (#112).

### Security note

The greeting path is **read-only** (one `icu_get_completed_sessions` GET; no writes to Intervals.icu) and **bounded**: the session query window is clamped server-side to **≤90 days** per the coach-mcp binding conditions (fpittelo/coach#82, binding condition 1). The banner dismissal write goes to the local MADR-0008 store only. No new secrets, no new network egress beyond the existing ICU/coach-mcp path.

## Consequences

- **Positive:** the coach initiates debriefs with zero new services or push infrastructure; the debrief loop opens conversationally inside the existing chat surface; dismissal is sticky and auditable in the MADR-0008 store; rate and latency budgets are explicit and testable; un-answered sessions decay gracefully into the weekly review instead of nagging.
- **Negative:** proactivity is limited to page-open moments (no offline reach); the greeting depends on ICU availability at page load (degrades to silence, and the session still folds into the weekly review); one more consumption contract on the yet-to-be-built `icu_get_completed_sessions` tool (fpittelo/coach#82).
- **Mitigations:** staleness rule guarantees eventual silence (≤2 prompt days per session); cache + capped retries bound ICU load; the fold-into-weekly-review path means no debrief is ever lost, only deferred; the tool dependency is tracked in fpittelo/coach#82 with binding security conditions.

## Rejected Alternatives (summary)

| Option | Why rejected |
|:---|:---|
| Server-side scheduler / cron (P2) | New operational surface in hardened containers; cannot deliver anything while the page is closed; violates KIS at n=1 (#163) |
| Web Push (P3) | Push subscription + VAPID + service-worker stack for one loopback user; conflicts with no-build-step frontend convention (ADR-006) (#163) |
| Email / mobile push (P4) | Health-adjacent prompts would leave the workstation boundary (nLPD minimization); mobile client does not exist (#163) |

## References

- Epic #161 — End-to-End Coaching Lifecycle Management (Epic 3 debrief loop): <https://github.com/fpittelo/coach-web/issues/161>
- Spike #164 (this record): <https://github.com/fpittelo/coach-web/issues/164>
- #163 — capability audit + PO decisions (greeting pattern, rate budget 5a, compensating controls): <https://github.com/fpittelo/coach-web/issues/163>
- #165 — UX rich-component contract, component 4 (pending-debrief banner): <https://github.com/fpittelo/coach-web/issues/165>
- MADR-0008 — persistence + `debrief_dismissal_flags` (reason enum per F10): [docs/adr/0008-persistence-data-classification.md](0008-persistence-data-classification.md)
- fpittelo/coach#82 — `icu_get_completed_sessions` tool + binding security conditions (read bounding ≤90 days, rate budget): <https://github.com/fpittelo/coach/issues/82>
- Repo-internal: ADR-006 (visual/streaming contract, docs/architecture.md §9), ADR-007 (local-first topology, docs/architecture.md §10).