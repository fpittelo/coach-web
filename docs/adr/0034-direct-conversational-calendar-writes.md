# MADR-0034: Direct Conversational Intervals.icu Calendar Writes

- **Status:** accepted
- **Date:** 2026-11-26
- **Deciders:** @fpittelo (PO direction 2026-11-26: direct single-event calendar CRUD in the `n=1` cockpit), @architect (specification, issue #466), @cyber-security (MADR-0008 verdict APPROVE-WITH-CONDITIONS C1–C8 and accepted residuals R1–R8, [comment 7199920667](https://github.com/fpittelo/coach-web/issues/466#issuecomment-7199920667)), @developer (implementation)
- **Supersedes:** the cancelled intermediate-UI-approval design of #463 / PR #464 (closed unmerged by PO decision 2026-11-26)
- **Related:** Sprint 39 marker #465 · routed follow-ups #467 (F1), `fpittelo/coach#127` (F2), #468, #469

## Context

Coach Web's agent could read the Intervals.icu calendar but could not edit it conversationally: the
model toolset carried the sidecar's READ family only, and calendar writes were reachable exclusively
through the human approval endpoint `POST /api/plan/approve`. The athlete-owner asked for the natural
cockpit flow — "move Thursday's threshold session to Friday", "write next week's ERG workouts with
explicit Watts", "delete that duplicate event" — to mutate the calendar directly in conversation,
without an intermediate approval card.

The change is deliberately narrow and proportionate to the `n=1` single-tenant posture (Cloud Run
IAP + in-app Google OIDC whitelist). It exposes exactly **three** single-event writes
(`intervals_create_event`, `intervals_update_event`, `intervals_delete_event`), adds no endpoint, no
secret, no ingress, no persistence and no UI surface. Every other upstream write stays invisible and
is refused pre-hub. Direct writes are not a new authorization model: the authenticated chat request
itself is the authorization for a single-event mutation, and human approval remains the boundary for
everything else (the 7 other `coach-mcp` writes, batch/activity/wellness writes, and
`POST /api/plan/approve`).

The design converts every prompt-level expectation that a *control* is required into a deterministic
server-side guard: mutation fan-out is budgeted (C1), update/delete targets must have been verified
by a same-run read (C2), the athlete id is pinned to self (C3), the dual-field workout erasure trap
is made unreachable (C4), the transport schema gate is hardened against external/cyclic references
(C5), and logging/doc claims are made truthful (C6/C7). The residual risks are explicitly accepted
by the PO under the 2026-11-26 `n=1` decision and recorded verbatim below (R1–R8).

## Decision Drivers

- **Owner ergonomics:** create/update/delete single calendar events — including explicit-Watt ERG
  `workout_doc` DSL — in the same conversational turn.
- **Least privilege:** exactly three single-event tools; default-deny allow-list at both the
  advertisement and dispatch boundaries; no batch calendar family.
- **Deterministic controls over prompt directives:** budgets, verified targets, pinned identity and
  preservation guards must be enforced server-side, before `hub.execute_tool`.
- **nLPD / Swiss privacy:** `coach-web` logs metadata only (tool name, structurally-safe event id,
  status, duration/counts) — never workout prose or tool-result bodies.
- **KIS & YAGNI:** no new endpoint, secret, migration, UI card or companion-repo change.

## Considered Options

1. **Intermediate approval card per single-event write (rejected — #463 / PR #464).** Adds a UI
   surface and a second confirmation dialog for the highest-frequency calendar operation. Closed
   unmerged by PO decision 2026-11-26 after the `@architect` + `@developer` consensus audit.
2. **Expose the full batch/calendar write family (rejected).** Disproportionate blast radius
   (unbounded fan-out, multi-field overwrite semantics) at `n=1`.
3. **Three direct single-event writes + deterministic server-side guards (chosen).** Smallest change
   that satisfies the conversational intent while keeping fan-out, target attribution, identity and
   data-loss failure modes bounded.

## Decision

Expose `COACH_MCP_EVENT_WRITE_TOOLS = {intervals_create_event, intervals_update_event,
intervals_delete_event}` and set `COACH_MCP_ALLOWED_TOOLS = COACH_MCP_READ_TOOLS |
COACH_MCP_EVENT_WRITE_TOOLS`, enforced identically in `filter_model_tools()` and
`_dispatch_allow_list_refusal()` (raw and namespaced). Enforce the following binding conditions.

### Binding conditions (C1–C7)

- **C1 — Per-user-action mutation budget.** `ICU_EVENT_MUTATION_BUDGET = 7` per agent run
  (`IcuEventMutationBudget`, mirroring `IcuReadBudget`). A call exceeding the cap is refused before
  `hub.execute_tool()` — never executed, never charged. The model-facing refusal and the log carry
  the tool name and counts only.
- **C2 — Server-side read-before-write gate.** `intervals_update_event` and
  `intervals_delete_event` are refused pre-hub unless the exact target `event_id` (flat
  `arguments["event_id"]` or wrapped `arguments["params"]["event_id"]`) was resolved by a
  **successful** `intervals_get_event` call in the same run. Success is strict: the returned payload
  must parse as a JSON `dict` without an `"error"` key — a failed `"Error fetching event..."`
  text, a hub error object or any non-object payload never qualifies. **Honest limitation:** C2 is a
  deterministic target-attribution / correctness control, **not** a prompt-injection barrier; an
  injected instruction can satisfy it by calling `intervals_get_event` first.
- **C3 — `athlete_id` pinned to self.** Before `hub.execute_tool` on all three event writes,
  `coach-web` deep-copies the arguments and pins `athlete_id` to the server-owned self value `"0"`:
  in the wrapped shape inside `params` (stripping any stray top-level `athlete_id`), otherwise
  top-level. A model-emitted `iXXXXX` can never target another athlete's calendar.
- **C4 — `description` / `workout_doc` preservation guard.** `intervals_update_event` is refused
  pre-hub when `description` is present (`not None`) and `workout_doc` is absent (`None`), because
  the sidecar then overwrites the calendar field and silently erases the compiled ERG workout. The
  refusal is static and self-correcting: resend both fields, or pass `workout_doc=""` for a
  prose-only calendar note.
- **C5 — Hardened local-only `$ref` schema gate.** The hub resolves only same-document
  `#/$defs/<Key>` references: no network or filesystem access for any other `$ref` (C5a); a visited
  key set plus a hard cap of 8 hops (C5b); unresolvable or cyclic references fail closed as
  non-permissive — no `response_format` injected — with one WARNING naming the tool only (C5c); and
  caller arguments are `copy.deepcopy`-ed before mutation (C5d). The default is injected into the
  declared container (`params` when wrapped, top level when flat) only when the dereferenced target
  declares `response_format` or is permissive (no properties, `additionalProperties` not `False`).
- **C6 — Two-tier nLPD logging hygiene.** `coach-web` logs metadata only: the allow-list refusal is
  reworded to `"Refused coach-mcp tool outside the allow-list: %s"` (tool name only), and every
  event-write execution/guard log records tool name, `event_id` only when structurally safe, outcome
  status and duration/counts — zero `name`, `description`, `workout_doc` or tool-result body. The
  system-level claim remains a **two-tier reality**: `coach-mcp`'s stderr audit trail records the
  redacted full arguments (including training prose) on every mutation; `redact_structure` removes
  only secret/token/e-mail material (residual R8, routed to F2). "Zero user/workout prose logged" is
  **not** asserted system-wide.
- **C7 — Documentation truthfulness.** `docs/security.md` B10 is rewritten (not amended) to replace
  the false pre-MADR-0025 claims with the server-owned encrypted thread posture and the scoped
  three-tool PO exception; B6 and the Local STRIDE Summary Table record the new path and its
  attribution caveat; `arc42/01` (C4 relationship label + tables), `02` and `03` (§1.1, §1.5, §3)
  are synchronised; `README.md` and `docs/{user_guide,admin_guide,e2e-checklist}.md` describe the
  direct-write flow and the `workout_doc.steps` check.

### PO-accepted residual risks (R1–R8, verbatim)

Recorded as accepted under the PO decision of 2026-11-26 and the single-owner (`n=1`) posture.

**R1 — Indirect prompt injection driving a calendar mutation (accepted).** Untrusted content in the
owner's own Intervals.icu event fields (or in the `fpittelo/coach` repo, until Epic #440 retires
`github-mcp`) is read back by `intervals_get_event` and appended **unsanitized** as `role="tool"`
content (`agent.py:1108`) — the sanitizer covers the 5 prompt blocks only, never tool results. The
model may then emit `intervals_update_event` / `intervals_delete_event`. *Source must already be
inside the owner's own data plane; impact is bounded to the owner's own calendar (training-plan
corruption / deletion — not exfiltration).* Partially mitigated by C2 (target attribution — **not**
an injection control) and by the prompt contract; **not eliminated**. Accepted at `n=1`.

**R2 — LLM intent / target misread (accepted).** The model may mutate the wrong event, or mutate when
the turn was hypothetical. Mitigations: C2 (deterministic verified-id gate), the "clarify before
mutating on hypothetical turns" directive, and truthful outcome reporting (a directive, not a
control). Residual stands.

**R3 — No in-app undo/rollback and no idempotency key (accepted).** `intervals_update_event` /
`intervals_delete_event` are declared `idempotent_hint=False`; a retry after a timeout can duplicate
a create, and there is no in-app rollback. Recovery is Intervals.icu's own calendar UI / history.
Bounded by C1 (mutation budget) and `AGENT_MAX_TOOL_ITERATIONS`.

**R4 — Dual-field `description`/`workout_doc` data-loss trap (mitigated by C4).** Verified semantics
in `coach_mcp/server.py::intervals_update_event`: `workout_doc` omitted + `description` present ⇒ the
calendar field is overwritten and the Intervals.icu-side compiled ERG workout is **erased**. C4 makes
that unreachable; residual = C4 over-constrains prose-only edits on structured events (remedy: resend
both fields, or an empty-string `workout_doc`).

**R5 — Repudiation attribution is by construction, not by identity (accepted).**
`coach_mcp/audit.py::_resolve_caller` returns the constant `"anonymous"`; the sidecar bearer-token
auth layer is not wired, so the audit entry records tool / athlete id / arguments / outcome /
correlation id but **not** the session identity. Attribution rests on (a) the single whitelisted
identity, (b) the server-owned encrypted transcript (MADR-0025 C2), (c) Intervals.icu's own event
history. **A MADR-0034 claim that the audit log attributes the mutation to the owner would be
inaccurate** (C6/C7). Follow-up F1 also closes this.

**R6 — The `coach-mcp` MCP endpoint is unauthenticated inside the Cloud Run instance (pre-existing;
accepted + routed).** `COACH_MCP_AUTH_TOKEN` appears **nowhere** in this repository;
`coach_mcp/config.py` defaults it to `""` ⇒ `BearerTokenMiddleware` is not applied ⇒ the endpoint
that now serves `intervals_delete_event` is reachable by any process in the instance's shared network
namespace (e.g. the `github-mcp` sidecar holding `GITHUB_TOKEN`), **bypassing the `coach-web`
allow-list entirely**. Pre-existing (the same endpoint already serves `intervals_create_event` for
`POST /api/plan/approve`); #466 raises its value, does not create it. → **F1**.

**R7 — Post-mutation drift against the SQLite plan state and the legacy GitHub plan Markdown
(accepted).** A direct write updates neither `plan_drafts` (`approved_week_row` →
`approved_week_digest`, `MorningBriefingCard`, `pending_debriefs`, `divergence_digest`) nor the legacy
`fpittelo/coach` plan Markdown ⇒ the prompt may assert a stale week and the GitHub plan may diverge
from the live calendar. In-scope mitigation: the `approved_week_digest` live-calendar precedence note
+ the "verify with `intervals_list_events`" directive. The divergence itself is **not** reconciled
(explicit PO cut).

**R8 — The sidecar audit log carries workout prose (pre-existing; accepted + routed).**
`coach_mcp/audit.py::build_audit_entry` records the redacted full arguments; `redact_structure`
strips only secret/token/e-mail material ⇒ `name`/`description`/`workout_doc` (training-plan prose,
personal data under nLPD) are written to the sidecar's stderr trail on every event mutation, and land
in the container/Cloud Run log retention. `coach-web`'s own logs must stay metadata-only (C6(a)) and
the system-level claim must be corrected (C6(b)). → **F2**.

## Out of Scope

- **F1 (routed to #467):** wire a bearer token for the `coach-mcp` MCP endpoint in the deploy lane
  (new Secret Manager secret, `COACH_MCP_AUTH_TOKEN` on the sidecar, client `Authorization`, D4 guard
  6 → 7 secrets). Closes R5 and R6.
- **F2 (routed to `fpittelo/coach#127`):** sidecar audit argument minimization (field names/hashes
  instead of workout values). Closes R8.
- **#468:** `POST /api/plan/approve` / `plan_approval.py` reconciliation; its pre-existing
  prefixed-output defect and the `plan_drafts` reconciliation stay untouched.
- **#469:** `propose_week_plan` / `build_batch_event_draft` weekly and legacy approval-flow changes.
- Zero changes to `src/coach_web/static/*`, FastAPI routes, DB/Alembic migrations,
  `plan_approval.py`, `WeeklyPlanDraft`, `coach-mcp` container env / D4 secret wiring (exactly
  **6** secrets unchanged), or companion repositories.

## Consequences

- **Positive:** the owner edits the calendar in conversation with explicit-Watt ERG workouts; the
  three writes are bounded, target-verified, self-pinned and erasure-safe by deterministic server
  code; the toolset remains default-deny; `coach-web` logs stay metadata-only; the security and
  arc42 documents now state the two-tier audit reality accurately.
- **Negative / accepted:** R1–R8 above; in particular, indirect prompt injection within the owner's
  own data plane is not eliminated, the sidecar audit still carries redacted workout prose until F2,
  and the audit trail is not identity-attributed until F1.
- **Verification:** `tests/test_mcp_hub.py` (C5 real Pydantic/FastMCP schemas, external/cyclic/deep
  refs, immutability), `tests/test_agent.py` (C1–C4, C6, prompt contract),
  `tests/test_microcycle.py` (digest precedence note), plus the zero-warning pre-flight and the five
  GitHub Actions PR checks.

## Links

- [MADR-0008 — Persistence & Data Classification](0008-persistence-data-classification.md)
- [MADR-0025 — Conversation Memory Model, Retention and Injection Contract](0025-conversation-memory-model-retention-and-injection-contract.md)
- [MADR-0026 — Admin Orientation and Agent Settings](0026-admin-orientation-and-agent-settings-prompt-precedence-and-sanitizer-contract.md)
- [MADR-0027 — Cloud Run Production Operating Model](0027-cloud-run-production-operating-model.md)
- [Security model — B6 / B10](../security.md)
- [arc42 — Context & Building Blocks](../architecture/arc42/01-context-and-building-blocks.md)
- [arc42 — Runtime & Delivery](../architecture/arc42/02-runtime-and-delivery.md)
- [arc42 — Cross-Cutting & Security](../architecture/arc42/03-cross-cutting-and-security.md)
