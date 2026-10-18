# MADR-0011: Quick-Reply Suggestions — Server-Owned Outcome Mapping on the `done` SSE Event

- **Status:** Accepted (2026-10-18 — @cyber-security APPROVED-WITH-CONTROLS, review recorded on fpittelo/coach-web#280; controls C-1…C-7 incorporated below and binding on #281)
- **Date:** 2026-10-18
- **Deciders:** @architect
- **Reviewed by:** @cyber-security (MADR-0008 security-impact routing — new API payload surface)
- **Tracked in:** fpittelo/coach-web spike #280, epic #206 (chat modernization); unblocks story #281 (quick-reply action pills)

> **Numbering note:** takes the next free number after MADR-0010. MADR-0001–0007 remain sections of `docs/architecture.md`; MADR-0008 and MADR-0010 are standalone files under `docs/adr/`; MADR-0009 lives as the MADR appendix of `docs/e2e-checklist.md`.

## Context

Epic #206 follow-up story #281 renders **quick-reply action pills** beneath completed coach messages — tappable one-click replies that reduce typing friction in the coaching conversation. The pills UI is blocked on this spike (#280): the backend contract for how contextual suggestions reach the client must be decided and specified first.

The current conversational wire contract (ADR-006, `POST /api/agent/stream`) emits typed `AgentEvent` SSE frames (`AgentEventType` Literal in `src/coach_web/agent.py`); every turn terminates with a `done` event whose data is `{message: str, iterations: int}`. The client's `done` handler (app.js) finalizes the turn — speaks the completed message and re-enables the composer.

Standing constraints that shape this decision:

- **n=1 local-first (ADR-007):** single whitelisted athlete, loopback lanes; operational burden is a core cost.
- **Rendering posture (ADR-006 §9):** assistant content renders sanitized markdown via `x-html`; **user-side and auxiliary surfaces render `x-text` only**; CSP `style-src 'self'`.
- **Untrusted-input culture:** model output is untrusted (STRIDE review #87 precedent); new payload surface triggers MADR-0008 security-impact routing — @cyber-security review REQUIRED before this spec is finalized.
- **KIS/YAGNI (home-governance §9):** smallest wire change that satisfies the need; no speculative machinery.
- **Swiss nLPD:** no new data collection, no persistence beyond the existing ephemeral client-owned history.

## Decision Drivers

1. **Minimal wire surface:** the `done` event already terminates every turn and already has a client handler; a new event type would add a Literal member, a client branch, and ordering concerns for zero gain.
2. **Untrusted-input minimization:** every model-generated string that reaches the DOM is an injection surface requiring server-side validation and rendering discipline; server-owned constants shrink that surface to zero.
3. **Latency contract (ADR-006):** no extra model round-trips or tool-loop iterations may be added per turn for a UI affordance.
4. **Deterministic testability:** the zero-warning gate culture favors contracts whose behavior is fully determined server-side.
5. **UI constraint:** pills are single-line, at most 4 per message.
6. **Backward compatibility:** the field must be additive-optional — older clients ignore it.

## Considered Options

#### Option O1: Server-owned outcome mapping, transported as optional `suggestions` on the `done` event (CHOSEN)

The server derives suggestions from the **turn outcome** (which structured card was emitted, or plain conversational completion) from a table of server-owned constant strings, and attaches them to the terminal `done` event as an optional field. No model involvement, no new event type.

#### Option O2: New `suggestions` SSE event type

Add a `suggestions` member to the `AgentEventType` Literal and emit a dedicated frame before `done`. **Rejected:** new Literal member + new client handler + frame-ordering/state concerns, for zero benefit over piggybacking on the event that already terminates the turn.

#### Option O3: Model-generated suggestions via prompt convention

Instruct the model to end replies with a parseable suggestions block; parse and validate server-side. **Rejected:** model output is untrusted (full validation burden), trailing-block parsing is fragile with streaming markdown, and prompt changes perturb the coaching persona for a UI affordance.

#### Option O4: Model-generated suggestions via a synthetic tool

Model calls a `propose_quick_replies` tool at end of turn; the loop intercepts, validates, attaches. **Rejected:** adds one tool-loop iteration (latency) per turn, same untrusted-input burden as O3, and complicates the tool allow-list boundary (#260) for zero user-visible gain today.

#### Option O5: Client-side-only suggestions

Client heuristics propose pills locally. **Rejected upfront (#280):** not contextual to the coach's actual reply.

## Decision

**Transport (AC2 — exact shape):** the terminal `done` `AgentEvent` data gains an **optional** field:

```jsonc
{
  "type": "done",
  "data": {
    "message": "<final assistant text>",
    "iterations": 1,
    "suggestions": ["Show this week's plan", "How did my last session go?"]  // OPTIONAL; absent = no pills
  }
}
```

- Field name: `suggestions` — `list[str]`, **1–4 items**, each **1–48 chars** after stripping.
- Absent field, empty list, and invalid payloads all mean "no pills" — the client never renders from an invalid shape because the server never emits one.

**Source:** server-owned constants in a new module `src/coach_web/suggestions.py` — an outcome→suggestions mapping keyed on **server-known outcome constants** (C-2: the mapping key is never model text), resolved through a **single construction choke point** (one function the agent loop calls; no ad-hoc dict assembly elsewhere). No model involvement at any point.

**Validation (defense in depth):** Pydantic model `SuggestionsPayload` (`list[str]` root, `max_length=4`, items `constr(min_length=1, max_length=48)`, stripped, control-character-free via field validator). Enforced **at the emission site** (C-3: the choke point validates via `SuggestionsPayload` — no raw dict passthrough, no model-dict shortcut); the constant tables are additionally validated at module load (fail-fast).

**Lifecycle / suppression rules (C-4: server-authoritative, per-turn, monotonic):** suppression is decided server-side per turn and is **monotonic across tool iterations** — once a suppressing outcome occurs anywhere in the turn (cards may be emitted mid-turn; `done` is always last), the turn carries no suggestions. The stream endpoint's own server-rendered `morning_briefing` emission (app.py) feeds the same decision — it is a turn outcome like any card. Precedence: if both a suppressing card outcome and a non-suppressing outcome occur in one turn, **suppression wins** (`rpe_request` + `debrief_summary` in one turn → none).

| Turn outcome | Suggestions |
|:---|:---|
| Plain conversational answer (no structured card) | default set (below) |
| `debrief_summary` card | debrief set (below) |
| `morning_briefing` card | briefing set (below) |
| `plan_proposal` / `week_plan` / `periodization_plan` / `rpe_request` cards | **none** — these cards carry their own explicit action affordances; pills would duplicate them |
| `error` (any path) or max-iterations exhaustion | **none** — the `done` events that follow errors carry no suggestions |

Initial server-owned sets (constants in `suggestions.py`, extended by ordinary PRs):

- default: `["Show this week's plan", "How did my last session go?", "What should I focus on?"]`
- debrief: `["That matches how it felt", "Not quite — let's adjust", "What's next week's focus?"]`
- briefing: `["What's today's session?", "I'm ready to train"]`

**Client contract (consumable by #281 without further design):** in the `done` handler, if `event.data.suggestions` is present, render the strings as pill `<button>` elements beneath the completed assistant message, inside a container with `role="group"` and `aria-label="Suggested replies"`. A click sends the string **verbatim** as a user message through the existing send path. Pills are cleared when the next send/stream starts (C-5d: absent/suppressed field renders nothing; clear-on-start is explicit). Pill state is **volatile client memory only** — never persisted to localStorage/IndexedDB or any store (C-6). Rendering is **`x-text` only — never `x-html`** (C-5a); pills are **siblings of the assistant bubble, never nested inside it** (C-5b: load-bearing containment invariant, contract-tested). Styling is classes-only under CSP `style-src 'self'`, no new dependencies (C-7).

## Security Review (MADR-0008 routing — recorded before acceptance)

**@cyber-security verdict: APPROVED-WITH-CONTROLS** (2026-10-18, review recorded on fpittelo/coach-web#280). Binding controls:

- **C-1:** any future source change (e.g., the model-generated source swap sketched under Consequences) **re-triggers MADR-0008** security review before merge — the wire shape may stay, the trust model may not silently change.
- **C-2:** single construction choke point; mapping keyed on server-known constants, never model text.
- **C-3:** emission-site `SuggestionsPayload` validation; no model-dict passthrough.
- **C-4:** server-authoritative, per-turn, monotonic suppression — including the endpoint-emitted `morning_briefing` and the precedence rule above.
- **C-5:** regression tests — (a) x-text-only rendering, (b) pills are siblings not nested in the assistant bubble, (c) error/max-iterations `done` events carry no suggestions, (d) absent/suppressed field renders nothing and clear-on-start is explicit.
- **C-6:** pill state is volatile client memory; no localStorage/IndexedDB persistence.
- **C-7:** classes-only CSP-safe styling; no new dependencies (no pip-audit/trivy/gitleaks delta).

## Security & Privacy (Swiss nLPD)

Suggestion strings are **server-owned constants** — not model output — so the untrusted-input class is eliminated at the source. Residual STRIDE surface (full review by @cyber-security recorded before acceptance, per MADR-0008):

- **T — Injection:** strings validated by Pydantic caps server-side; rendered `x-text` only; no `x-html` anywhere in the pill path.
- **S — Spoofing/UI confusion:** pills are semantically distinct from coach messages (real `<button>` elements in a labeled group), never rendered inside assistant bubble content.
- **R / I / D / A:** no new data flows, no persistence (client-ephemeral, cleared on next turn), no external calls, no new dependencies.
- **nLPD:** no new data collection; generic constant strings, no personal data.

## Consequences

- **+** Minimal diff: one optional field on an existing event + one constants module + one Pydantic model; no new event type, no prompt changes, no tool-loop changes, no latency cost.
- **+** Zero untrusted input; deterministic contract tests (caps, suppression table, mapping) fit the zero-warning gate.
- **+** Backward compatible: additive-optional; older clients ignore the field.
- **−** Suggestions are outcome-driven sets, not free-text-conversational — acceptable for n=1 coaching flows; model-generated suggestions can later be added **behind the same wire shape** (source swap, contract unchanged) — **but per C-1 such a source swap re-triggers MADR-0008 review before merge**.
- **−** The suppression table must be kept in sync when new structured card types are introduced (ordinary PR duty, contract-tested).

## Open Questions

- None blocking. Exact pill styling/visual grammar is #281's scope (frontend), constrained by the client contract above.