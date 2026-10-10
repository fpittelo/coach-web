# ♿ Accessibility

## Live-region posture (issues #85–#86, #149)

The chat console is contracted for screen readers as follows (pinned by
`tests/test_static_assets.py` — source assertions, no Node toolchain):

- The message history is a `role="log"` region: structural chat semantics
  with implicit polite additions announcements for newly added messages.
  The whole-log explicit `aria-live` was removed in #86 — a token-by-token
  stream would spam screen readers.
- A visually-hidden `role="status"` region (`aria-live="polite"`) carries
  the deliberate announcements, from exactly two `announce()` call sites:
  short static phase labels per phase transition, and the completed final
  assistant message on stream done — never per token.
- The final-message announcement is **markdown-stripped plain text**
  (#149 AC2): `stripMarkdown()` removes emphasis, list, heading, code-fence
  and link markers so the announcement reads as natural prose. The visual
  rendering path (`renderMarkdown` → marked + DOMPurify, #81) is untouched
  and remains the sole on-screen source of truth.
- Announcement text derives only from already-rendered assistant content —
  no new data exposure (Swiss nLPD).

---

## AT verification procedure — `role="log"` / `role="status"` (#149 AC1)

**Status: PENDING EXECUTION (pending-PO).**

Actual screen-reader verification requires human/AT execution and cannot be
automated by the source-assertion suite: the tests pin the DOM posture, but
only a screen reader can confirm what is actually spoken and how often.
Execute the procedure below, then record the outcome (tool, date, per-check
result) in the #149 issue closing comment.

### What to verify

| # | Check | Expected result |
|---|---|---|
| 1 | Final-message announcement | The completed final assistant message is announced **exactly once** via the `role="status"` region, as plain prose — no `**`, list markers, backticks or raw URLs from link syntax (#149 AC2) |
| 2 | No double announcement | The `role="log"` history's implicit `aria-live="polite"` does **not** produce a second, duplicate announcement of the same final message right after the status-region announcement, and no per-token re-announcements during streaming |
| 3 | Phase labels | Each phase transition (waiting / streaming / tooling) announces its short static label exactly once; the error phase announces its label |
| 4 | Stream end | No announcement storm at stream end; focus returns to the composer without an extra spoken artifact |

### Tool matrix

| Tool | Browser | Notes |
|---|---|---|
| NVDA | Firefox | Primary Windows matrix; enable the speech viewer to confirm wording and count |
| VoiceOver | Safari | Primary macOS matrix; use the caption panel to read announcements as text |
| Orca | Firefox | Optional Linux cross-check |

### Steps

1. Start the AT, open the dev lane (`http://localhost:8000`), focus the composer.
2. Send a prompt that yields a markdown-rich reply (bold text, a list, a
   link — e.g. "Give me an overview of next week's training plan").
3. Listen through the whole stream: count the announcements (phase labels,
   then the final message).
4. Confirm check 1: the final message is spoken once, as plain text.
5. Confirm check 2: no duplicate final-message announcement from the log
   region and no per-token chatter during streaming.
6. Repeat once with the second AT matrix from the table above.
7. Record tool, date and per-check outcome in the #149 closing comment.

**If check 2 fails** (double announcement observed): remediate by
neutralizing the log's implicit live semantics (e.g. `aria-live="off"` on
the log region, keeping the `role="status"` region as the sole announcer),
re-run this procedure, and record the remediation with the outcome.

---

## Inline card status notes (issue #168, per the #165 contract)

Conversation cards surface user-action outcomes (approval results, per-day
write status) through an inline, visually-hidden **`role="status"` note
element** — the `approval-note` slot pattern, declared with `role="status"`
so its `x-text` changes are announced programmatically (WCAG 4.1.3) without
any JS announcement contract change. This is a DOM pattern, not an
`announce()` call site: the frozen two-call-site `announce()` contract
(#149) is untouched, and card insertion itself is still covered by the
final-message announcement (the agent's final message prose-references the
card it emitted). The weekly plan card (#168) ships its note as a
`role="status"` region; its per-day status chips always pair the state word
with the color — color is never the sole carrier. Pinned by
`tests/test_static_assets.py` source assertions.

---

_Last updated: 2026-10-10 (issue #168 — inline card status notes subsection; live-region posture summary + AT verification procedure, pending execution)._