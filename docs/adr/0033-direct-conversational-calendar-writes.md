# MADR-0033: Direct Conversational Calendar Writes for Single Intervals.icu Events

- **Status:** accepted
- **Date:** 2026-11-26
- **Deciders:** @fpittelo (PO: revised decision, issue #463 comment `7187717065` — direct writes, no approval card), @developer (implementation)
- **Supersedes:** the approval-card expansion direction proposed in #463 comment `7186510639` (rejected by the PO as overengineered). Historical accepted MADRs are unchanged; this record is the current scope.

## Context

Coach Web's agent could already **read** the Intervals.icu calendar through
`coach-mcp` read tools, but every calendar write was gated behind the
`PlanProposal` approval card and `POST /api/plan/approve`. The first attempt to
add update/delete (issue #463) extended that card with an `operation` field, a
second-click delete confirmation, an approval parser rewrite, telemetry-cache
eviction and new UI states.

The Product Owner reviewed that design and **rejected it as overengineered**.
The revised binding decision (issue #463 comment `7187717065`) is direct
conversational management for **single events**: an unambiguous athlete chat
request is the authorization to invoke the appropriate environment tool, with no
approval card and no second confirmation. The multi-day weekly/season plan
proposal flows are explicitly out of scope and stay unchanged.

Two transport facts constrain the implementation:

1. The `coach-mcp` single-event mutation input models
   (`CreateEventInput`/`UpdateEventInput`/`DeleteEventInput`) are
   `extra="forbid"` and do **not** declare `response_format`. The hub previously
   injected `response_format=json` into every coach-mcp call, which the real
   hub rejects. Injection must be gated on the advertised, resolved input
   schema, at both flat and nested `params` shapes.
2. The model catalogue and the dispatch boundary are separate enforcement
   points; both must reflect the same explicit allow-list so a prompt-injected
   tool name cannot bypass the catalogue filter.

## Decision Drivers

- **PO intent:** direct, low-friction calendar management; reject UI overhead
  (KIS/YAGNI).
- **Least privilege:** expose exactly the 3 single-event mutations; every other
  coach-mcp write (batch, activity, wellness) stays denied by default.
- **Truthfulness:** the model must report the real upstream outcome, never
  invent success or failure.
- **Target correctness:** update/delete must resolve and read the exact event
  before mutating.
- **Single-user reality:** one athlete, one whitelisted identity. The owner's
  chat request is the authorization (n=1 trust boundary).

## Considered Options

### Option 1 (chosen): Direct writes through the existing catalogue and dispatch allow-list

Add `COACH_MCP_EVENT_WRITE_TOOLS` (`intervals_create_event`,
`intervals_update_event`, `intervals_delete_event`) and
`COACH_MCP_ALLOWED_TOOLS = COACH_MCP_READ_TOOLS | COACH_MCP_EVENT_WRITE_TOOLS`.
`filter_model_tools` and `_dispatch_allow_list_refusal` both use the combined
allow-list. `MCPClientHub` injects `response_format` only where the resolved
input schema declares it (flat or nested `params`). The system prompt tells the
model that a clear request authorizes the direct call, that update/delete must
first resolve and read the target, and that it must report the tool's actual
result.

- Pros:
  - Minimal diff; no new endpoint, synthetic tool, persistence, UI or approval
    protocol.
  - Default-deny is preserved for the other 7 coach-mcp write tools at both
    enforcement points.
  - The `response_format` fix also hardens existing read transport.
- Cons:
  - Relies on prompt discipline for intent parsing and target selection.
  - No UI affordance, preview or confirmation.

### Option 2 (rejected by PO): Approval-card expansion

Extend `PlanProposal` with `operation`/`event_id`, an update/delete card, a
delete confirmation and a parser rewrite.

- Pros: explicit confirmation for destructive actions.
- Cons: the PO judged it overengineered; the UI/validation surface triples for a
  single-user app. Rejected.

### Option 3 (rejected): Dedicated write endpoint or synthetic CRUD tool

- Pros: narrow models per operation.
- Cons: duplicates the dispatch guard and adds a new API/synthetic surface for
  no user benefit; violates KIS. Rejected.

## Decision

Adopt **Option 1**. Direct single-event create/update/delete through the
existing model catalogue and dispatch allow-list; multi-day plan proposals
unchanged. `response_format` injection is schema-gated everywhere, including
nested `params`.

## Consequences

- **Positive:** the athlete can add, move, retitle, retarget and delete a single
  calendar event conversationally; other writes remain structurally unavailable;
  read transport keeps its JSON default.
- **Negative / residual risks (accepted by the PO under the n=1 trust boundary):**
  - **Prompt-error risk.** The model interprets intent and selects the target.
    A misread request can mutate the wrong event; mitigation is the
    read-before-update/delete instruction and the small tool surface, not a
    confirmation UI.
  - **No rollback.** A confirmed change is immediate; recovery is a manual
    opposite change or an Intervals.icu edit.
  - **No idempotency key.** A retried or duplicated tool call can re-apply an
    update or re-attempt a delete.
  - **Attribution.** Direct writes leave no GitHub commit trail; attribution
    rests on the single whitelisted identity and the Intervals.icu event
    history.
  - **No plan-Markdown mirror.** Update/delete do not touch the GitHub plan
    documents, so they can drift from the live calendar.
- **Out of scope (unchanged):** `PlanProposal`/plan/week/periodization proposal
  flows, `POST /api/plan/approve`, UI, persistence, batch-upsert conversion,
  `coach-mcp` source/release changes, and unrelated `github-mcp` privileges.
