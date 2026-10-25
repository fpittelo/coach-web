# MADR-0025: Admin Orientation and Agent Settings — Prompt Precedence and Sanitizer Contract

- **Status:** accepted
- **Date:** 2026-10-25
- **Deciders:** @architect, @fpittelo
- **Reviewed by:** @cyber-security (MADR-0008 security-impact routing — persistent owner-authored text injected into the LLM system prompt on a health-data lane; APPROVE-WITH-CONDITIONS C1–C6, review recorded on fpittelo/coach-web#359)
- **Tracked in:** fpittelo/coach-web#359 (epic), Sprint 30 marker; implemented by #361 (persistence + seam), #362 (API), #363 (UI); memory epic #346 plugs into the same seam in Sprint 31 and MUST honour the precedence contract below

## Context

Coach's personality and coaching style are hard-coded: the system prompt is the static `DEFAULT_SYSTEM_PROMPT` constant (`src/coach_web/agent.py:112`). Steering Coach — more concise, stricter on recovery, a different tone — requires a code deployment. The owner wants a simple admin surface to edit orientation and response preferences from the UI, with immediate effect and a reset path.

The architectural forces at play:

- **The system prompt is a trust boundary.** Anything placed in it steers every downstream model decision, including tool calls that touch C2 health data (`readiness`, `fitness`, calendar) and the training-plan workflow. Owner-authored orientation text must therefore complement — never override — the immutable safety/tool-grounding core, and must survive the owner pasting untrusted text (e.g. a "coach prompt" copied from a website) into the settings form.
- **Immediate effect is already the natural behaviour.** `CoachAgent` is instantiated fresh per request (`app.py:613`) and the prompt is assembled per request (`app.py:662-755`). No reload machinery is needed; the work is making per-request assembly settings-aware through a single seam.
- **Classification constraint (MADR-0008).** Orientation text may carry health-adjacent phrasing ("be strict when my fatigue is high") and is transmitted to the third-party model provider (OpenRouter) on **every turn**. Encrypting it at rest would not prevent that disclosure; it would be security theatre.
- **Two epics share one seam.** The memory epic (#346, Sprint 31) will insert a fenced memory-data block into the same prompt assembly. The block order must be fixed NOW so the second epic plugs in without refactoring the first.
- **n=1 deployment.** Single whitelisted owner; the prod lane is loopback-only. Controls must be proportionate — no multi-user machinery.

## Decision Drivers

- The immutable core (tool-grounding, wattage precision, safety, data-handling rules) must never be editable from the UI — style steers tone, not permissions.
- Prompt-injection defence must be structural (cap + fence + escape + fixed precedence), not lexical filtering; phrase reject-lists were evaluated and rejected as detection theatre (cyber Q1).
- Log hygiene per #354: no orientation content in logs — metadata only (length, hash, timestamp).
- Swiss nLPD: health-adjacent *semantics* live in fixed enum columns, never in free text; the third-party transmission is disclosed at save time.
- KIS/YAGNI (home-governance §9): single user, single row, single surface; no admin role split, no version history, no moderation pre-pass.
- The memory epic must be able to insert its block without touching this decision's implementation.

## Considered Options

### Option 1: Settings-free-text appended verbatim to the system prompt

- Pros: smallest possible diff; zero new schema beyond one text column.
- Cons: unbounded; no precedence control; orientation could override safety rules (last-block-wins); no home for structured preferences; memory epic would have no defined insertion point.

### Option 2: Presets/DSL only (fixed personality presets, no free text)

- Pros: strongest control — no owner text reaches the prompt at all.
- Cons: fails the PO's core use case (personal philosophy, e.g. "focus on power-to-weight, be pragmatic"); presets cannot express style; owner falls back to code deployments — the problem this epic exists to remove.

### Option 3: `agent_settings` store + single `assemble_system_prompt` seam with fixed precedence and a two-site sanitizer (chosen)

- Pros: closes the trust boundary structurally; one pure seam that both this epic and the memory epic plug into; enums carry health-adjacent semantics; C1-plaintext classification is honest because transmission is disclosed; reset-to-default is a row delete.
- Cons: a sanitizer + fencing layer must be maintained; the owner can still persist text that steers style in unforeseen ways (residual risk, accepted below).

## Decision Outcome

Chosen option: **"Option 3 — `agent_settings` store + single assembly seam with fixed precedence"**, because it is the only option that delivers free-text steering *and* keeps the safety core structurally override-proof, while giving the memory epic a pinned insertion point.

### The schema

`agent_settings` (Alembic migration, reversible): single row per athlete — `orientation` C1 **plaintext**, capped at **1500 chars** post-normalisation (CHECK constraint), `verbosity` nullable enum CHECK `IN ('concise','standard','detailed')`, `tone` nullable enum CHECK `IN ('strict','balanced','supportive')`, `updated_at`. Mirrors the `athlete_objectives` conventions.

### The precedence contract (pinned — binding on the memory epic #346)

`assemble_system_prompt(...)` — one pure, unit-tested seam — emits blocks in exactly this order:

1. **Immutable core** — `DEFAULT_SYSTEM_PROMPT`; safety, tool-grounding, data-handling. Never editable, never relocated.
2. **Owner-orientation block** — fenced, delimited, escaped, explicitly labelled *non-authoritative style guidance*. Sanitized at assembly (the DB row is treated as untrusted input).
3. **Structured-pref directives** — short fixed server-side mappings from the enum columns; sensitive intent ("strict when fatigued") is expressed here, never as free text.
4. **Per-request digests** — existing objectives digest (stays *"authoritative"*) and periodization.
5. **Memory block (future, #346)** — fenced *data, never instructions*; slots **below orientation, above the re-assert clause**. Orientation is not memory: orientation lives in `agent_settings`, memory in its own tables.
6. **Re-assert clause (LAST)** — a short restatement of the immutable safety/tool/data rules; the final block, so no earlier block structurally has the last word.

### The sanitizer contract (one pure function, two call sites — cyber C2)

Applied at **save** (Pydantic → 422) and at **assembly** (read-time, DB row untrusted): NFKC normalise → strip C0/C1 control chars except newline/tab → cap at 1500 → neutralise `<|…|>`-style tags, `<system>`-style tags, and `^\s*(system|assistant|user|tool)\s*:` role tokens → emit inside the envelope. **No phrase reject-list** ("ignore previous instructions" filters were rejected as theatre — cap + fence + escape is the control; the real backstop is that tools stay gated by human-approval flows regardless of prompt content). A phrase-level warning at save is permissible, warn-only.

### Failure posture

- Settings read failure at request time → **fail open to the immutable core** (chat must not depend on settings-store health; extends the existing digest-degrade boundary). Regression-tested (cyber C6).
- No row → defaults (core-only; byte-equivalent to today's prompt — verified by regression test).
- Oversize/corrupted row at assembly → clamped/sanitized by the read-time sanitizer pass.
- Concurrent saves → last-writer-wins (single owner).
- Migration failure at boot → fail-closed (existing Alembic behaviour).

### API, UI, audit

- `GET /api/admin/agent-settings` (defaults when no row), `PUT` (validate + sanitize → 422 on violation, echoes normalized text), `DELETE` (reset to default = row delete). Behind the existing default-deny `/api/*` middleware; POST/PUT/DELETE-only mutations with the SameSite+TrustedHost+JSON-body CSRF posture (no token store). 422 detail is field-only, no value echo (#352 posture).
- UI: an **Orientation & agent preferences** section inside the existing settings modal (`index.html:1062`) — one surface, zero new routing/auth surface. Textarea with maxlength + live counter, two segmented enum controls, Save, and "Reset to Coach defaults" with confirm. Save-time disclosure: "Orientation text is sent to the model provider on every turn" (cyber C4).
- Audit: journal line on change carries athlete, length, sha256 prefix, `updated_at` — **never content** (cyber C3, #354 posture). `updated_at` + reset path are the recovery story; version history is YAGNI.

### Cyber conditions record (APPROVE-WITH-CONDITIONS)

| # | Condition | Disposition |
| :--- | :--- | :--- |
| C1 | Single assembly seam; fixed precedence documented; orientation after core, re-assert last | The precedence contract above; enforced by pinning tests |
| C2 | One sanitizer, two call sites (save + assembly); cap enforced at both; block fenced/escaped/non-authoritative | The sanitizer contract above |
| C3 | No orientation content in logs — length + hash only | Audit spec above |
| C4 | C1 plaintext **with save-time disclosure** of third-party transmission; health-adjacent semantics via enums only | Disclosure line + enum design |
| C5 | Block-order + "orientation is not memory" rules recorded in the memory epic before its grooming closes | Recorded on #346 (2026-10-25); restated in precedence item 5 |
| C6 | Settings-read failure extends the degrade boundary; regression test | Failure posture above; test in #361 AC4 |

## Consequences

- **Positive:** the owner steers Coach's tone/style from the UI with immediate effect and no restart; the safety core is structurally protected by precedence and fencing; the memory epic plugs into a finished seam; health-adjacent intent is expressible without free text; the reset path is a single DELETE.
- **Negative:** a sanitizer and fencing layer must be maintained and tested at two call sites; the precedence contract is now a cross-sprint obligation — the memory epic (and any future prompt-feeding feature) must respect it; free-text steering can still produce unforeseen style effects within the fence.
- **Mitigations:** the seam is pure and pinned by order-assertion tests; the re-assert clause is the structural last word; reset-to-default is always one call away; the disclosure keeps the nLPD posture honest.

### Residual risk accepted (verbatim, from the cyber review)

- The owner can persist free text that steers coach style; string filtering cannot prevent content-level persuasion. The mitigation is the immutability of the safety/tool-grounding core and the human-in-the-loop tool gates, not detection of malicious phrasing.
- Orientation text is transmitted to the third-party model provider (OpenRouter) on every turn; encrypting at rest would not prevent that disclosure — the owner is informed at save time.
- A settings change during an in-flight SSE turn takes effect on the next turn only.
- A read failure of the settings store degrades to the immutable core prompt rather than blocking chat.

### Deliberately rejected (YAGNI)

Step-up re-auth · CSRF token store · version history/diff/rollback UI · encrypting orientation · LLM moderation pre-pass · prompt-reload watcher/cache · preset DSL beyond the two enums · separate admin role · multi-user roles/admin separation · prompt A/B testing · versioned prompt library · per-conversation overrides · any change to tool gating or the plan/debrief approval flows.