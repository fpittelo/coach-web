/* Coach Web — Alpine.js chat console and plan approval controller.
 *
 * Consumes the typed SSE stream exposed by POST /api/agent/stream through
 * fetch + ReadableStream (the native SSE browser API cannot POST) and posts
 * approved plans to POST /api/plan/approve. The conversation is client-owned:
 * every turn replays the last ≤ 10 transcript messages as history inside the
 * request body — message and history content never appear in a URL or query
 * string (#79, Swiss nLPD). "New chat" (resetChat) is a full client-side
 * reset: it aborts any in-flight stream and clears every state slice without
 * transmitting anything.
 *
 * Rendering posture (ADR-006 §9, gated by STRIDE #87): user data is only
 * ever bound with x-text. Assistant messages are the sole HTML-rendered
 * surface, bound to renderMarkdown() — the default-allowlist DOMPurify
 * sanitizer over marked-parsed markdown, re-sanitizing the accumulated raw
 * string on every render (#81).
 *
 * Inline plan card (#82): the proposed plan renders as a single
 * conversation card beneath the assistant message that proposed it —
 * planMessageIndex pins the card to the proposing message, and a new
 * proposal replaces the previous card (KIS: no stacked plan history). The
 * approve/reject POST contract is unchanged: {plan: <PlanProposal>} to
 * POST /api/plan/approve; reject dismisses the card and clears plan state.
 *
 * Weekly plan card (#168): the proposed 7-day microcycle renders as a
 * second single-active conversation card pinned via the weekPlan /
 * weekMessageIndex pair (one state pair per card type, so both cards can
 * coexist). Approval posts {week, dates} to POST /api/week/approve — the
 * write is NOT atomic (#163): per-day status chips track proposed →
 * pushing… → approved ✓ / write failed, and a partial failure offers the
 * retry-failed-days subset. Adjust prefills the composer and never
 * auto-sends (PO Q8).
 *
 * Periodization card (#182): the coach-proposed season phase plan renders
 * as a third single-active conversation card pinned via the
 * periodizationPlan / periodizationMessageIndex pair. The card shows the
 * phases (type, name, dates, focus) plus the coach's rationale; Approve
 * posts {plan} to POST /api/periodization/approve — the server re-validates
 * and persists through the #167 repository, so no phase is ever stored
 * without the athlete's explicit approval (#87 C7). Reject dismisses
 * without persisting. The manual settings editor is gone (PO pivot):
 * phases are coach output, not athlete input.
 *
 * Debrief capture (#189): the coach's rpe_request event opens the session
 * check-in card (rpe/rpeMessageIndex pair, #82 replacement rule) — an RPE
 * stepper 1–10 (two real buttons around an aria-live value, #165 a11y)
 * plus optional notes. LOG CHECK-IN posts {plan_session_key, rpe, notes}
 * to POST /api/debrief; the server encrypts the notes (C2 Fernet) and
 * persists per athlete, and the card collapses to the one-line summary
 * carried by the role="status" note. A 401 surfaces "Please sign in." and
 * a 503 renders the fixed encryption vocabulary (DEBRIEF_ERROR_CODES
 * mirror, generic fallback kept). The pending-debrief banner (MADR-0010)
 * is fetched once at init from GET /api/debrief/pending — passive,
 * dismissible, metadata-only; LOG IT prefills the composer (never
 * auto-send, PO Q8).
 *
 * Debrief summary & ICU mirror (#190): the debrief_summary SSE event
 * carries the server-built planned-vs-executed card (built from the
 * fetched ICU compliance report — never agent-claimed numbers, AC1)
 * pinned via the debriefSummary/debriefSummaryMessageIndex pair (#82
 * replacement rule). The card is purely informational: headline, range,
 * report counts and per-session rows with word+token severity chips
 * (#165 — never color alone). The check-in card gains an opt-in
 * "Mirror to Intervals.icu" consent folded into LOG CHECK-IN (no second
 * confirmation card); the POST carries mirror_to_icu and a mirror
 * failure surfaces the server's fixed soft warning in the submitted
 * note while the debrief stays saved (AC3).
 *
 * Settings error reasons (#181): the owner-facing settings saves (objectives
 * PUT, periodization approval) surface the server's structured, owner-safe
 * 422 body {code, message} — the message renders only when its code is in
 * the client's fixed SETTINGS_REASON_CODES vocabulary, else the generic
 * text (fail-safe). A 401 surfaces "Please sign in." with a real /auth/login
 * link in the settings modal (pairs with #180). Every other surface keeps
 * the strict #142 posture: no exception text, no input echo (nLPD).
 *
 * Tool activity indicator (#199): the per-tool status chips are gone — the
 * tooling status line is the single compact activity indicator, carrying
 * the friendly static label "Coach is checking your training data…" while
 * MCP calls are in flight. Tracking is id-based: tool_call adds the call
 * id, tool_result releases it, the last release returns the waiting dots,
 * and finishStream clears the tracker so a failed stream never leaves a
 * lingering indicator. Raw coach-mcp__* tool names are never rendered or
 * stored (nLPD/#142 posture); typed and transport errors keep surfacing
 * through the #84 paths.
 *
 * Stream phases (#84): the UI state carries an explicit plain-string phase —
 * idle | waiting | streaming | tooling | error (KIS: no state-machine
 * framework) — driving the thinking dots, status line and non-blocking
 * error banner. Typed agent errors and transport failures are separate code
 * paths: a typed error keeps the stream alive (agent.py emits error THEN
 * done), while a transport failure or the 90 s no-event watchdog finishes
 * it and leaves a visible, non-blocking banner behind. Status and banner
 * strings are static or server-error text only — never message content or
 * secrets (Swiss nLPD).
 *
 * Auto-follow (#85): the chat log follows the stream only while the
 * viewport is within 60 px of the bottom (stickToBottom, armed by a
 * passive scroll listener); farther away, token arrivals and transcript
 * mutations never move the viewport, and scrolling back near the bottom
 * re-arms following automatically — no "jump to latest" pill (KIS cut).
 * Token-time scrolls are instant (behavior "auto"), and the stylesheet
 * disables looping animations under prefers-reduced-motion.
 *
 * Composer ergonomics (#83): the composer is a single-row textarea that
 * auto-grows with content up to the stylesheet's ~5-line cap (internal
 * scroll beyond) and shrinks back when the input is cleared — one
 * $watch("input") hook drives both directions. Enter sends, Shift+Enter
 * inserts a newline, and an IME composition guard (event.isComposing)
 * keeps the composition-confirm Enter from sending. The send-disabled
 * bindings (empty input / in-flight stream) are unchanged.
 *
 * Onboarding & accessibility (#86): an empty transcript shows the
 * onboarding block — hero monogram, one-line welcome and 3 static
 * endurance starter chips (PO Q2/Q7: domain-correct client-side strings,
 * no personal data). prefillChip() fills the composer and focuses it —
 * never auto-send (PO Q8). The whole-log aria-live is replaced by a
 * visually-hidden status region: a $watch("phase") speaks a short static
 * label per phase transition and the done handler speaks the completed
 * final message — never per token (AC3). The final-message announcement is
 * markdown-stripped plain text (#149 AC2): the status region reads natural
 * prose while the visual x-html path (renderMarkdown → marked + DOMPurify)
 * stays untouched. finishStream() returns focus to
 * the composer (disabling the textarea on send drops it to <body>)
 * without stealing it from a deliberate target such as a plan-card
 * button (AC4).
 *
 * Floating composer (#207, epic #206 phase 1): the composer becomes an
 * enclosed rounded capsule — textarea on top, a single utility dock row
 * inside the capsule (clip + tools left, mic + submit right). The
 * clip/tools/mic buttons are DISABLED placeholders with honest "Coming
 * soon" tooltips — no dummy handlers (Phases 2–4). The submit control
 * morphs into a Stop square while a stream is in flight: stopStreaming()
 * drives the SAME client-side AbortController path resetChat() uses
 * (finishStream finalizes the partial assistant message and clears the
 * stream state; no error banner, no new server endpoint). The greeting
 * (empty thread, no pending-debrief banner) carries a time-of-day
 * salutation bound via x-text only (ADR-006 §9) — the client holds no
 * athlete name, so the salutation stays generic.
 *
 * Single dispatch path (#207 AC 1.3): the form's submit event is the ONLY
 * send() entry point. The keydown handler routes a plain Enter through
 * requestSubmit() into the same submit handler (the IME guard returns
 * before the route), so keydown and implicit submit can never both fire
 * send() — the double-send landmine from the #206 study. While a stream is
 * in flight the submit handler branches to stopStreaming() instead: the
 * textarea is disabled then, so the keydown route is dead and the Stop
 * click is the only submit event source.
 *
 * Message bubble grammar (#279): the terminal-style role labels give way
 * to a Sendbird-inspired grammar — assistant messages render as
 * left-aligned rounded neutral cards with the coach monogram avatar, a
 * "Coach" sender label and a client-side receipt time (HH:MM); user
 * messages render as right-aligned accent bubbles. Every message is
 * stamped with a client-clock receipt at push time (stampReceipt); the
 * receipt drives the bubble timestamp and the centered date dividers
 * between messages whose receipt days differ (dateDividerLabel). The
 * stamps are CLIENT-SIDE ONLY (nLPD AC2): in-memory state, stripped from
 * the history replay by stripReceipt (the wire contract stays {role,
 * content} pairs), never persisted, never transmitted. No chat component
 * library — pure CSS + markup restructuring over the vendored stack.
 *
 * Quick-reply pills (#281, MADR-0011): the terminal done event may carry
 * an optional server-owned `suggestions` array (1–4 constant strings —
 * never model output). The pills render beneath the assistant message
 * that completed the turn: real <button> elements in a role="group"
 * container with aria-label "Suggested replies", siblings of the
 * assistant bubble — never nested inside its x-html subtree (C-5b).
 * Labels bind via x-text only (C-5a); the shape guard
 * (normalizeSuggestions) renders only from a valid payload, so an
 * absent/suppressed field renders nothing (C-5d). A click submits the
 * pill text verbatim through the SINGLE send() dispatch path (the form's
 * submit event via requestSubmit — no double-send) and the pills are
 * cleared when the next send/stream starts (C-5d) and by resetChat().
 * Pill state is volatile client memory only — never persisted (C-6) —
 * and the styling is classes-only under CSP style-src 'self' (C-7).
 */

// 90 s no-event watchdog (AC3, #84): a backstop ABOVE the server's own
// deadlines — the 60 s OpenRouter completion timeout (config.py
// OPENROUTER_TIMEOUT_SECONDS) and the 30 s MCP tool-call timeout — so it
// only fires when the stream is truly wedged: no agent event of any kind
// for 90 s.
const WATCHDOG_TIMEOUT_MS = 90000;

// Auto-follow proximity band (#85): the log follows the stream only while
// the viewport is within 60 px of the bottom; farther away, following
// pauses so scrolling up to re-read is never hijacked (AC1). Scrolling
// back near the bottom re-arms following automatically (AC2).
const STICK_THRESHOLD_PX = 60;

// Step-profile geometry (#235 AC1): the plan card renders one SVG bar per
// step with the bar height proportional to the step's target intensity,
// normalized against the plan peak. A step without any power target
// renders a neutral stub instead of inventing a value.
const STEP_PROFILE_STUB_PCT = 15;

// Week-card label vocabulary (#168): static client-side strings for the
// per-day status chips (word + color, never color alone — #165) and the
// compact day labels rendered in the day table.
const WEEK_DAY_STATUS_WORDS = {
  proposed: "proposed",
  pushing: "pushing…",
  approved: "approved ✓",
  failed: "write failed",
};

const WEEK_DAY_LABELS = {
  mon: "Mon",
  tue: "Tue",
  wed: "Wed",
  thu: "Thu",
  fri: "Fri",
  sat: "Sat",
  sun: "Sun",
};

// Debrief-card severity vocabulary (#190): static client-side words for the
// server-assigned severity tokens (word + tokenized color, never color alone
// — #165). "missed" names the critical outcome (the planned work did not
// happen); "drift" names a completed session whose executed structure
// diverged from the plan.
const DEBRIEF_SEVERITY_WORDS = {
  ok: "ok",
  warn: "drift",
  critical: "missed",
};

const WEEK_DAY_FULL_NAMES = {
  mon: "Monday",
  tue: "Tuesday",
  wed: "Wednesday",
  thu: "Thursday",
  fri: "Friday",
  sat: "Saturday",
  sun: "Sunday",
};

const MONTH_LABELS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

// Settings reason vocabulary (#181): the client mirror of the server's fixed
// enum (coach_web.errors.SettingsReason). The settings 422 body is
// {code, message}; the server-provided message renders ONLY when its code is
// in this vocabulary — an unknown or missing code (vocabulary drift, a
// non-settings error shape) falls back to the current generic text
// (fail-safe). Fixed vocabulary only: no exception text, no input echo (nLPD).
const SETTINGS_REASON_CODES = {
  coverage_target_date: true,
  overlap: true,
  invalid_dates: true,
  invalid_values: true,
};

function settingsReasonMessage(body, fallback) {
  // Owner-safe 422 rendering (#181): return the server-provided message when
  // the body carries a KNOWN reason code and a non-empty string message;
  // anything else returns the caller's generic fallback unchanged. The
  // hasOwnProperty guard keeps prototype keys out of the vocabulary check.
  if (
    body &&
    typeof body.code === "string" &&
    Object.prototype.hasOwnProperty.call(SETTINGS_REASON_CODES, body.code) &&
    typeof body.message === "string" &&
    body.message
  ) {
    return body.message;
  }
  return fallback;
}

// Debrief error vocabulary (#189): the client mirror of the server's fixed
// encryption-failure enum (coach_web.errors.EncryptionReason). The debrief
// 503 body is {code, message}; the server-provided message renders ONLY when
// its code is in this vocabulary — an unknown or missing code falls back to
// the generic text (fail-safe). Fixed vocabulary only: no exception text, no
// input echo, never key material (nLPD).
const DEBRIEF_ERROR_CODES = {
  key_unavailable: true,
};

function debriefReasonMessage(body, fallback) {
  // Owner-safe 503 rendering (#189): same guard shape as
  // settingsReasonMessage — known code + non-empty string message, else the
  // caller's generic fallback unchanged.
  if (
    body &&
    typeof body.code === "string" &&
    Object.prototype.hasOwnProperty.call(DEBRIEF_ERROR_CODES, body.code) &&
    typeof body.message === "string" &&
    body.message
  ) {
    return body.message;
  }
  return fallback;
}

function parsePayload(event) {
  try {
    const raw = typeof event === "string" ? JSON.parse(event) : JSON.parse(event.data);
    if (raw && typeof raw.data === "object" && raw.data !== null) {
      return raw.data;
    }
    return raw || {};
  } catch (err) {
    return {};
  }
}

// Markdown → plain text for the live-region announcement (#149 AC2): the
// done handler speaks the completed final message, and raw markdown source
// ("**bold**", "- item", "```fences```") reads as syntax noise. The stripper
// removes the common emphasis/list/heading/link/fence markers and keeps the
// content, so the announcement reads as natural prose. KIS: a fixed regex
// chain, no new dependency, NOT a markdown parser — the visual rendering
// path (renderMarkdown → marked + DOMPurify) is untouched and remains the
// sole on-screen source of truth. Known limits: table pipes and horizontal
// rules pass through, paired intra-word underscores are read as emphasis,
// and raw HTML tags are not stripped (the sanitizer handles those on
// screen only).
function stripMarkdown(text) {
  return text
    .replace(/```[a-z]*\n?/gi, "") // fenced code blocks: drop the fence lines
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1") // links/images: keep the label
    .replace(/`([^`]*)`/g, "$1") // inline code: keep the content
    .replace(/(\*\*|__)(.*?)\1/g, "$2") // strong: **bold** / __bold__
    .replace(/(\*|_)(.*?)\1/g, "$2") // emphasis: *italic* / _italic_
    .replace(/~~(.*?)~~/g, "$1") // strikethrough
    .replace(/^[ \t]{0,3}#{1,6}[ \t]+/gm, "") // ATX headings
    .replace(/^[ \t]{0,3}[-*+][ \t]+/gm, "") // bullet list markers
    .replace(/^[ \t]{0,3}\d+\.[ \t]+/gm, "") // ordered list markers
    .replace(/^[ \t]{0,3}>[ \t]?/gm, "") // blockquote markers
    .trim();
}

// Receipt timestamp helpers (#279): client-side only (nLPD AC2).
// stampReceipt records the client clock at push time into the in-memory
// message; stripReceipt rebuilds the {role, content} wire shape for the
// history replay so the timestamp is never transmitted. Nothing is
// persisted — the transcript stays ephemeral client state.
function stampReceipt(message) {
  message.receipt = Date.now();
  return message;
}

function stripReceipt(message) {
  return { role: message.role, content: message.content };
}

// Quick-reply pill shape guard (#281, MADR-0011): the server never emits an
// invalid suggestions payload (Pydantic-capped server-side), but the client
// renders only from a valid shape — an array of non-empty strings capped at
// four. Anything else degrades to no pills (C-5d: absent/suppressed → none).
// The strings are server-owned constants rendered via x-text only (C-5a).
function normalizeSuggestions(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item) => typeof item === "string" && item.trim() !== "")
    .slice(0, 4);
}

function blankGoalForm() {
  // Blank goal form factory (N4, PR #171): shared by the initial state, the
  // add-goal action and the empty-profile load path — the settings form is
  // never empty, so a blank primary goal renders before any load completes.
  return {
    objective_type: "outcome",
    title: "",
    description: "",
    target_metric: "",
    targetValue: null,
    target_date: "",
  };
}

function coachApp() {
  return {
    messages: [],
    input: "",
    streaming: false,
    streamEpoch: 0,
    // Explicit stream phase (#84): idle | waiting | streaming | tooling |
    // error. A plain string guarded by the existing compound checks — no
    // state-machine framework (KIS).
    phase: "idle",
    statusText: "",
    // Persistent, non-blocking error banner text (#84). Static or
    // server-error text only — never message content or secrets (nLPD).
    // Survives finishStream so a failure stays visible; cleared on the next
    // send or reset.
    errorBanner: "",
    // Sign-in action for the banner (#180): armed only when a stream fails
    // with 401 (expired/missing session) — the banner then carries a real
    // /auth/login link next to the message. Every other banner writer keeps
    // it off, so the action never outlives its 401 cause.
    errorBannerSignin: false,
    // 90 s no-event watchdog (#84): timer id plus the epoch and assistant
    // bubble index of the stream that armed it.
    watchdog: null,
    watchdogEpoch: 0,
    watchdogIndex: -1,
    // Auto-follow flag (#85): armed by proximity (STICK_THRESHOLD_PX) via
    // the scroll listener on the log. Defaults to armed — a fresh,
    // non-scrollable log sits at the bottom by definition.
    stickToBottom: true,
    thoughts: [],
    tools: [],
    plan: null,
    // Index of the assistant message that proposed the active plan (#82):
    // pins the single inline card beneath the proposing message. -1 when no
    // plan is active; a new proposal overwrites both slots (AC4: one card).
    planMessageIndex: -1,
    approval: { state: "idle", message: "" },
    // Weekly plan card (#168): the proposed 7-day microcycle renders as a
    // single conversation card pinned to the proposing message via the
    // weekPlan/weekMessageIndex pair (#82 replacement rule — a new
    // week_plan event replaces the card). Per-day write statuses ride
    // their own map keyed by ISO date (proposed | pushing | approved |
    // failed — #165 chips, word + color never color alone); the card-level
    // approval state mirrors the plan card's plain-string machine.
    weekPlan: null,
    weekMessageIndex: -1,
    weekApproval: { state: "idle", message: "" },
    weekDayStatus: {},
    // Periodization card (#182): the coach-proposed season phase plan
    // renders as a third single-active conversation card pinned via the
    // periodizationPlan/periodizationMessageIndex pair (#82 replacement
    // rule — a new periodization_plan event replaces the card). The payload
    // carries the validated plan (its phases) plus the coach's rationale;
    // Approve posts {plan} to POST /api/periodization/approve — the
    // human-in-the-loop gate (#87 C7) — and Reject dismisses without
    // persisting. The manual settings editor is gone (PO pivot): phases are
    // coach output, not athlete input.
    periodizationPlan: null,
    periodizationMessageIndex: -1,
    periodizationApproval: { state: "idle", message: "" },
    // Debrief capture (#189): the coach's rpe_request event opens the
    // session check-in card (rpe/rpeMessageIndex pair, #82 replacement
    // rule). The stepper value and notes ride their own slices; LOG
    // CHECK-IN posts {plan_session_key, rpe, notes} to POST /api/debrief
    // and the card collapses to the one-line summary carried by the
    // role="status" note. pendingDebriefs is the passive banner list
    // fetched once at init (GET /api/debrief/pending, MADR-0010) —
    // metadata only (session key + title, never notes or RPE).
    rpe: null,
    rpeMessageIndex: -1,
    rpeApproval: { state: "idle", message: "" },
    rpeValue: 5,
    rpeNotes: "",
    pendingDebriefs: [],
    // Debrief summary card (#190): the server-built planned-vs-executed
    // card arrives on the debrief_summary SSE event — pinned to the
    // message that fetched it (#82 replacement rule). mirrorToIcu is the
    // athlete's per-check-in consent to mirror the debrief to
    // Intervals.icu (#190 AC3) — strictly opt-in, reset whenever a new
    // check-in opens or the card is dismissed.
    debriefSummary: null,
    debriefSummaryMessageIndex: -1,
    mirrorToIcu: false,
    // Morning briefing card (#262): the server-rendered greeting card
    // arrives on the morning_briefing SSE event — pinned to the message
    // that opened the stream (#82 replacement rule). The request is
    // gated by the client-side date flag (R3 — not a security boundary).
    morningBriefing: null,
    morningBriefingMessageIndex: -1,
    // Quick-reply pills (#281, MADR-0011): the server-owned suggestion
    // strings delivered on the terminal done event, pinned to the message
    // that completed the turn (#82 grammar). Volatile client memory only
    // (C-6) — cleared when the next send/stream starts (C-5d) and by
    // resetChat(); never persisted.
    suggestions: [],
    suggestionsMessageIndex: -1,
    // Visually-hidden live-region text (#86 AC3): phase labels and the
    // completed final message — never per token. Static strings or final
    // message content only (nLPD).
    liveAnnouncement: "",
    // Static endurance starter chips (#86 AC1, PO Q2/Q7): domain-correct
    // client-side strings — no personal data in onboarding (nLPD).
    starterChips: [
      "Plan my next training week",
      "How ready am I today?",
      "Review last week's training load",
    ],
    controller: null,
    // Objectives settings panel (#166, AC1): toggled from the identity bar.
    // The form mirrors the ObjectiveProfile API shape — goals[0] is the
    // primary goal, goals[1:] the secondaries; optional numerics ride as
    // strings in the inputs and normalize to null on save. All bindings are
    // x-text/x-model (structured Pydantic→HTML, no x-html).
    settingsOpen: false,
    settingsSaving: false,
    settingsMessage: "",
    // Sign-in action for the settings note (#181 AC2): armed only when a
    // settings API call fails with 401 — the note then carries a real
    // /auth/login link next to "Please sign in." (pairs with #180). Every
    // other writer keeps it off, so the action never outlives its 401 cause.
    settingsSignin: false,
    objectivesForm: {
      goals: [blankGoalForm()],
      weeklyAvailabilityHours: 0,
      priorityDisciplines: "",
    },

    init() {
      this.$watch("messages", () => this.scrollToBottom());
      // Auto-grow composer (#83): every input mutation — typing, paste,
      // and the programmatic clears in send()/resetChat() — re-fits the
      // textarea height. The measurement runs on the next tick so the
      // x-model DOM write lands before scrollHeight is read; measuring
      // synchronously would read the stale value and never shrink.
      this.$watch("input", () => this.$nextTick(() => this.autoGrowTextarea()));
      // Live-region announcements (#86 AC3): every phase transition speaks
      // a short static label to the visually-hidden status region; the
      // completed final message is announced by the done handler. Tokens
      // never announce.
      this.$watch("phase", (phase) => this.announcePhase(phase));
      // Pending-debrief banner (#189, MADR-0010): fetched once at init —
      // the only non-SSE data path (#165). Passive: any failure degrades
      // to no banner, never to an error surface.
      this.loadPendingDebriefs();
      this.$nextTick(() => {
        const log = this.$refs.log;
        if (!log) {
          return;
        }
        // Proximity arming (#85): every scroll of the log re-evaluates the
        // follow flag — within 60 px of the bottom (re-)arms following
        // (AC2, self-healing), farther away pauses it (AC1). The listener
        // is passive: the handler never calls preventDefault.
        log.addEventListener("scroll", () => this.updateStickToBottom(), {
          passive: true,
        });
        this.updateStickToBottom();
      });
    },

    scrollToBottom() {
      // Proximity-gated follow (#85): the flag is armed by the scroll
      // listener on the log. While it is false — the user scrolled farther
      // than 60 px from the bottom — token arrivals and transcript
      // mutations never move the viewport (AC1).
      if (!this.stickToBottom) {
        return;
      }
      this.$nextTick(() => {
        const log = this.$refs.log;
        if (log) {
          // Instant scroll (AC3): behavior "auto" is never animated, so a
          // rapid token stream cannot stack eased scrolls into jank.
          log.scrollTo({ top: log.scrollHeight, behavior: "auto" });
        }
      });
    },

    updateStickToBottom() {
      // Proximity arming (#85): within 60 px of the bottom ⇒ follow;
      // farther ⇒ pause. Scrolling back near the bottom re-arms following
      // without any action beyond the scroll itself (AC2, self-healing —
      // no "jump to latest" pill, KIS cut locked in #85).
      const log = this.$refs.log;
      if (!log) {
        return;
      }
      this.stickToBottom =
        log.scrollHeight - log.scrollTop - log.clientHeight <= STICK_THRESHOLD_PX;
    },

    renderMarkdown(text) {
      // Sanitizer helper — the ONLY path from message text to HTML
      // (ADR-006 §9, STRIDE #87 conditions C1/C3). marked parses the
      // accumulated raw string and DOMPurify sanitizes the result with its
      // default allowlist — no allow-list additions of any kind, so
      // script/style/iframe/object/embed/form, event handlers, javascript:
      // URIs and every Alpine x-*/@*/:* directive are stripped. The Alpine
      // x-html binding calls this on every render with the FULL accumulated
      // content — never per-token sanitized fragments — so a construct split
      // across tokens (<scr|ipt>) is only ever sanitized as a whole, and
      // DOMPurify repairs unclosed tags mid-stream.
      return window.DOMPurify.sanitize(window.marked.parse(text));
    },

    autoGrowTextarea() {
      // Auto-grow composer (AC1, #83; #207 recap): reset to the intrinsic
      // single-row height first so scrollHeight reflects the current
      // content, then size the box to fit it. The border correction reads
      // the computed border widths — scrollbar-blind, unlike a
      // client/offset box delta, which would absorb a horizontal
      // scrollbar's height (~15px) and over-size the box if one ever
      // appeared (#130 review). overflow-y: auto only engages past the
      // stylesheet's ~8-line max-height cap — never at the fitted size.
      // Clearing the input (send / reset) shrinks the box back through the
      // same $watch("input") path.
      const textarea = this.$refs.composer;
      if (!textarea) {
        return;
      }
      const style = getComputedStyle(textarea);
      const borders =
        parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
      textarea.style.height = "auto";
      textarea.style.height = textarea.scrollHeight + borders + "px";
    },

    onComposerKeydown(event) {
      // Enter-to-send (AC2, #83; single dispatch #207): plain Enter
      // prevents the newline and routes through the form's submit event —
      // the ONE send() dispatch path (keydown and implicit submit can
      // never both fire send()). Shift+Enter fails the !event.shiftKey
      // half and falls through to the textarea's default action (newline
      // inserted). The IME composition guard returns early while an input
      // method editor is composing (e.g. Japanese kana conversion) — the
      // composition-confirm Enter must never send.
      if (event.isComposing) {
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        this.$refs.composerForm.requestSubmit();
      }
    },

    onComposerSubmit() {
      // Single dispatch entry (#207 AC 1.3): the form's submit event —
      // reached from the submit button's activation AND from the keydown
      // handler's requestSubmit() route — is the only send() call site.
      // While a stream is in flight the submit control IS the Stop button
      // (AC 1.5): the branch drives the client-side abort instead. The
      // textarea is disabled during a stream, so the keydown route is dead
      // then and the Stop activation is the only submit source.
      if (this.streaming) {
        this.stopStreaming();
        return;
      }
      this.send();
    },

    stopStreaming() {
      // Stop control (AC 1.5, #207): the user-visible stop affordance
      // drives the SAME client-side AbortController path resetChat() uses
      // — no new server endpoint. finishStream() aborts the in-flight
      // request AFTER finalizing the stream state (phase idled, watchdog
      // cleared), so the partial assistant message stays exactly as
      // streamed; the resulting AbortError rejection is swallowed
      // explicitly in startStream's catch (a deliberate local abort — no
      // error banner).
      if (!this.streaming) {
        return;
      }
      // The in-flight assistant placeholder is the last message (send()
      // is the only stream starter and is guarded by this.streaming). An
      // empty bubble (Stop pressed before the first token) is dropped —
      // unless an inline card is pinned to it (a card may arrive before
      // any token; removing the bubble would re-pin the card to the wrong
      // message).
      const index = this.messages.length - 1;
      const bubble = this.messages[index];
      const pinnedCard =
        this.planMessageIndex === index ||
        this.weekMessageIndex === index ||
        this.periodizationMessageIndex === index ||
        this.rpeMessageIndex === index ||
        this.debriefSummaryMessageIndex === index ||
        this.morningBriefingMessageIndex === index;
      this.finishStream();
      if (bubble && bubble.role === "assistant" && !bubble.content && !pinnedCard) {
        this.messages.splice(index, 1);
      }
    },

    get greetingLine() {
      // Time-of-day salutation (#207 AC 1.2): bound via x-text only
      // (ADR-006 §9 — the greeting is plain text, never HTML). The client
      // holds no athlete name (nLPD posture; no backend change in Phase 1
      // scope), so the salutation stays generic — a future name source
      // must ride this same x-text path, never an HTML binding.
      const hour = new Date().getHours();
      if (hour < 12) {
        return "Good morning";
      }
      if (hour < 18) {
        return "Good afternoon";
      }
      return "Good evening";
    },

    receiptTime(message) {
      // Client-side receipt time (#279 AC1): HH:MM in the viewer's locale,
      // derived from the in-memory receipt stamp — never persisted, never
      // transmitted (nLPD AC2).
      if (!message.receipt) {
        return "";
      }
      return new Date(message.receipt).toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
      });
    },

    dateDividerLabel(index) {
      // Date divider (#279 AC3): a plain localized date labels the first
      // message and every message whose client receipt day differs from
      // the previous one. Client-side only (nLPD AC2) — derived from the
      // in-memory receipt stamps.
      const message = this.messages[index];
      if (!message || !message.receipt) {
        return "";
      }
      const day = new Date(message.receipt).toDateString();
      const previous = this.messages[index - 1];
      if (previous && previous.receipt && new Date(previous.receipt).toDateString() === day) {
        return "";
      }
      return new Date(message.receipt).toLocaleDateString(undefined, {
        year: "numeric",
        month: "long",
        day: "numeric",
      });
    },

    announce(text) {
      // Write the visually-hidden status region (#86 AC3). The only two
      // call sites are announcePhase (phase labels) and the done handler
      // (the final message, markdown-stripped #149) — tokens never announce.
      this.liveAnnouncement = text;
    },

    announcePhase(phase) {
      // Phase labels (#86 AC3): one short static announcement per phase
      // transition — never per token. Static strings only: no message
      // content, no secrets (nLPD). "idle" is silent: the done handler
      // has already announced the final message.
      const phaseLabels = {
        waiting: "Waiting for the coach",
        streaming: "The coach is responding",
        tooling: "The coach is checking your training data",
        error: "The coach hit an error",
      };
      if (phaseLabels[phase]) {
        this.announce(phaseLabels[phase]);
      }
    },

    prefillChip(chip) {
      // Starter chips (AC2, #86; PO Q8): prefill-and-focus — NEVER
      // auto-send. The composer is filled and focused; sending stays an
      // explicit Enter / Send action. The $watch("input") hook grows the
      // textarea to fit the prefilled text.
      this.input = chip;
      this.$refs.composer.focus();
    },

    sendSuggestion(suggestion) {
      // Quick-reply pill (#281 AC2, MADR-0011): the click submits the pill
      // text verbatim as a user turn IMMEDIATELY — through the SINGLE
      // send() dispatch path (the form's submit event, #207 AC 1.3). The
      // composer is filled and the form submitted via requestSubmit(), the
      // same route the Enter key uses; there is no direct send() call here,
      // so keydown, submit button and pill activation can never double-send.
      // The submit rides $nextTick so requestSubmit() observes the submit
      // button AFTER Alpine's reactive DOM write clears its empty-input
      // :disabled binding (a disabled default button blocks requestSubmit).
      // The pills are cleared by startStream() when the submit opens the
      // new stream (C-5d).
      this.input = suggestion;
      this.$nextTick(() => this.$refs.composerForm.requestSubmit());
    },

    // ------------------------------------------------------------------
    // Objectives settings panel (#166, AC1). The panel loads the persisted
    // ObjectiveProfile on open and saves it via PUT /api/objectives. All
    // bindings are structured x-text/x-model. Error surfaces (#181): the
    // owner-safe structured reason message renders for known codes, the
    // generic text is the fallback, and a 401 surfaces the sign-in prompt
    // with a real /auth/login link — no other server detail is echoed
    // (nLPD, #142 posture unchanged for everything else).
    // ------------------------------------------------------------------

    toggleSettings() {
      if (this.settingsOpen) {
        this.closeSettings();
      } else {
        this.openSettings();
      }
    },

    openSettings() {
      this.settingsOpen = true;
      this.settingsMessage = "";
      this.settingsSignin = false;
      this.loadObjectives();
      // Move focus into the dialog (keyboard accessibility): the panel is
      // tabindex="-1" so it is programmatically focusable without entering
      // the tab order.
      this.$nextTick(() => {
        const panel = this.$refs.settingsPanel;
        if (panel) {
          panel.focus();
        }
      });
    },

    closeSettings() {
      // Guard: the window-level Escape binding fires even when the panel is
      // closed — closing must never steal focus in that case.
      if (!this.settingsOpen) {
        return;
      }
      this.settingsOpen = false;
      const toggle = this.$refs.settingsToggle;
      if (toggle) {
        toggle.focus();
      }
    },

    trapSettingsFocus(event) {
      // Focus trap (a11y, PR #171 N3): Tab cycles within the dialog —
      // Shift+Tab on the first focusable wraps to the last, Tab on the last
      // wraps to the first; preventDefault keeps focus from escaping to the
      // page behind the modal. Keyboard-only logic, no motion involved.
      // Hidden controls (x-show, e.g. the primary goal's remove button) and
      // disabled buttons are not Tab stops and never take the wrap.
      const panel = this.$refs.settingsPanel;
      if (!panel) {
        return;
      }
      const focusables = Array.from(
        panel.querySelectorAll("button, input, select, textarea, a[href]")
      ).filter((el) => el.offsetParent !== null && !el.disabled);
      if (focusables.length === 0) {
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    },

    formFromProfile(profile) {
      // API → form: goals[0] is the primary goal, the rest are secondaries.
      const toGoalForm = (goal) => ({
        objective_type: (goal && goal.objective_type) || "outcome",
        title: (goal && goal.title) || "",
        description: (goal && goal.description) || "",
        target_metric: (goal && goal.target_metric) || "",
        targetValue: goal && goal.target_value !== null && goal.target_value !== undefined
          ? goal.target_value
          : null,
        target_date: (goal && goal.target_date) || "",
      });
      if (!profile) {
        return {
          goals: [blankGoalForm()],
          weeklyAvailabilityHours: 0,
          priorityDisciplines: "",
        };
      }
      return {
        goals: [profile.primary_goal].concat(profile.secondary_goals || []).map(toGoalForm),
        weeklyAvailabilityHours:
          profile.weekly_availability_hours === null || profile.weekly_availability_hours === undefined
            ? 0
            : profile.weekly_availability_hours,
        priorityDisciplines: (profile.priority_disciplines || []).join(", "),
      };
    },

    loadObjectives() {
      fetch("/api/objectives")
        .then(async (response) => {
          if (!response.ok) {
            // 401 (#181 AC2): an expired or missing session is a sign-in
            // problem, not a data problem — surface the prompt and arm the
            // /auth/login link. Any other failure keeps the generic text.
            if (response.status === 401) {
              this.settingsSignin = true;
              this.settingsMessage = "Please sign in.";
              return;
            }
            this.settingsMessage = "Could not load objectives.";
            return;
          }
          const body = await response.json();
          this.objectivesForm = this.formFromProfile(body.profile);
        })
        .catch(() => {
          this.settingsMessage = "Could not load objectives.";
        });
    },

    payloadFromForm() {
      // Form → API: empty optional numerics/dates serialize as null (the API
      // validates strict types — an empty string would be a 422); the
      // comma-separated disciplines input becomes a trimmed, filtered list.
      const fromGoalForm = (goal) => ({
        objective_type: goal.objective_type,
        title: goal.title,
        description: goal.description.trim() === "" ? null : goal.description,
        target_metric: goal.target_metric.trim() === "" ? null : goal.target_metric,
        target_value:
          goal.targetValue === "" || goal.targetValue === null
            ? null
            : Number(goal.targetValue),
        target_date: goal.target_date === "" ? null : goal.target_date,
      });
      const goals = this.objectivesForm.goals.map(fromGoalForm);
      const disciplines = this.objectivesForm.priorityDisciplines
        .split(",")
        .map((item) => item.trim())
        .filter((item) => item !== "");
      return {
        primary_goal: goals[0],
        secondary_goals: goals.slice(1),
        weekly_availability_hours: Number(this.objectivesForm.weeklyAvailabilityHours) || 0,
        priority_disciplines: disciplines,
      };
    },

    saveObjectives() {
      if (this.settingsSaving) {
        return;
      }
      this.settingsSaving = true;
      this.settingsMessage = "";
      this.settingsSignin = false;
      fetch("/api/objectives", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(this.payloadFromForm()),
      })
        .then(async (response) => {
          if (!response.ok) {
            // 401 (#181 AC2): an expired or missing session is a sign-in
            // problem, not a data problem — never "check the values".
            if (response.status === 401) {
              this.settingsSignin = true;
              this.settingsMessage = "Please sign in.";
              return;
            }
            // 422 (#181 AC1): the owner-safe structured reason body
            // {code, message} renders for known codes; unknown/missing codes
            // fall back to the generic text (fail-safe). The generic detail
            // body is never read (nLPD #142 posture for everything else).
            const body = await response.json().catch(() => ({}));
            this.settingsMessage = settingsReasonMessage(
              body,
              "Could not save objectives. Check the values and retry."
            );
            return;
          }
          const body = await response.json();
          this.objectivesForm = this.formFromProfile(body.profile);
          this.settingsMessage = "Objectives saved.";
        })
        .catch(() => {
          // Transport failure: the generic text — no server detail echoed.
          this.settingsMessage = "Could not save objectives. Check the values and retry.";
        })
        .finally(() => {
          this.settingsSaving = false;
        });
    },

    addGoal() {
      this.objectivesForm.goals.push(blankGoalForm());
    },

    removeGoal(index) {
      // The primary goal (index 0) is never removable — the profile requires
      // exactly one primary goal; the remove button is hidden for it.
      if (index > 0) {
        this.objectivesForm.goals.splice(index, 1);
      }
    },

    send() {
      const message = this.input.trim();
      if (!message || this.streaming) {
        return;
      }
      // Receipt stamp (#279 AC2): client clock at push time — in-memory
      // only, stripped from the history replay (never transmitted).
      this.messages.push(stampReceipt({ role: "user", content: message }));
      this.input = "";
      this.startStream(message);
    },

    briefingFlagStale() {
      // First-load-of-the-day flag (#262, R3): client-derived and NOT a
      // security boundary — a forged or absent flag only re-renders the
      // same read-only, server-derived card. UTC day granularity; storage
      // unavailability fails open (the card may re-render, harmlessly).
      try {
        const today = new Date().toISOString().slice(0, 10);
        return window.localStorage.getItem("coachBriefingDate") !== today;
      } catch (error) {
        return true;
      }
    },

    markBriefingShown() {
      // Marked at REQUEST time (#262): repeated sends on the first load
      // never re-request the briefing (the server derives statelessly and
      // the readiness read is TTL-cached — but one request per day is the
      // contract). A failed stream loses the day's briefing (accepted).
      try {
        const today = new Date().toISOString().slice(0, 10);
        window.localStorage.setItem("coachBriefingDate", today);
      } catch (error) {
        // Storage unavailable — the flag stays stale; the card is read-only.
      }
    },

    startStream(message) {
      // Stale-event hardening: every SSE handler below captures the epoch at
      // registration and bails unless it is still current. resetChat() aborts
      // the fetch mid-flight, but frames already buffered can still dispatch
      // afterwards — and a bare `this.streaming` check would even let them
      // through once a NEW stream is active again. The epoch half rejects
      // anything from an older stream; the `this.streaming` half rejects late
      // same-stream events after done/the catch handler ended it. One guard
      // idiom, applied consistently to every stream handler.
      this.streamEpoch += 1;
      const epoch = this.streamEpoch;
      this.streaming = true;
      // AC4: the waiting phase (pulsing dots) is set synchronously — before
      // the fetch is even dispatched, hence before any SSE event arrives.
      this.phase = "waiting";
      this.statusText = "Connecting";
      // A new attempt clears the previous failure banner (AC3: retry by
      // sending again, no reload) and any sign-in action armed by it (#180).
      this.errorBanner = "";
      this.errorBannerSignin = false;
      this.thoughts = [];
      this.tools = [];
      this.plan = null;
      this.planMessageIndex = -1;
      this.approval = { state: "idle", message: "" };
      // The weekly card follows the same replacement posture (#82): a new
      // stream clears the previous card; the agent's week_plan event (if
      // any) re-pins a fresh one.
      this.weekPlan = null;
      this.weekMessageIndex = -1;
      this.weekApproval = { state: "idle", message: "" };
      this.weekDayStatus = {};
      // The periodization card (#182) follows the same replacement posture:
      // a new stream clears the previous card; the agent's periodization_plan
      // event (if any) re-pins a fresh one.
      this.periodizationPlan = null;
      this.periodizationMessageIndex = -1;
      this.periodizationApproval = { state: "idle", message: "" };
      // The check-in card (#189) follows the same posture: a new stream
      // clears the previous card; the agent's rpe_request event (if any)
      // re-pins a fresh one.
      this.rpe = null;
      this.rpeMessageIndex = -1;
      this.rpeApproval = { state: "idle", message: "" };
      // The morning briefing card (#262) follows the same posture: a new
      // stream clears the previous card; the morning_briefing event (if
      // any) re-pins a fresh one.
      this.morningBriefing = null;
      this.morningBriefingMessageIndex = -1;
      // Quick-reply pills (#281, C-5d): a new send/stream start clears the
      // previous turn's pills explicitly — the done event of the new turn
      // (if any) re-pins a fresh set.
      this.suggestions = [];
      this.suggestionsMessageIndex = -1;

      // Morning briefing request (#262): the FIRST stream of the day
      // carries briefing=true; the flag is marked at request time so
      // repeated sends never re-request it.
      const requestBriefing = this.briefingFlagStale();
      if (requestBriefing) {
        this.markBriefingShown();
      }

      // History replay (#79): the conversation is client-owned. Snapshot the
      // PRIOR turns before pushing the in-flight assistant placeholder — the
      // just-submitted user message travels as `message` (never duplicated in
      // history) and the empty placeholder is never replayed. The server
      // caps history at 10 entries; mirror the cap here so oversized
      // transcripts are trimmed client-side first. Receipt stamps (#279
      // AC2) are stripped: the wire contract stays {role, content} pairs —
      // client-side timestamps are never transmitted (nLPD).
      const history = this.messages.slice(0, -1).slice(-10).map(stripReceipt);

      this.messages.push({ role: "assistant", content: "" });
      const index = this.messages.length - 1;
      // Receipt stamp (#279 AC2): client clock at push time, recorded after
      // the history snapshot — the placeholder is never replayed and the
      // stamp never travels.
      stampReceipt(this.messages[index]);

      const controller = new AbortController();
      this.controller = controller;
      const handlers = this.streamHandlers(index, epoch);
      // Arm the 90 s no-event watchdog before the fetch dispatches, so a
      // connection that never establishes is covered too (AC3).
      this.armWatchdog(epoch, index);

      fetch("/api/agent/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: message,
          history: history,
          briefing: requestBriefing,
        }),
        signal: controller.signal,
      })
        .then(async (response) => {
          if (!response.ok || !response.body) {
            // The status rides the error (#180): a 401 arms the banner's
            // sign-in action in the catch below.
            const failure = new Error("Stream unavailable (" + response.status + ")");
            failure.status = response.status;
            throw failure;
          }
          await this.consumeSse(response.body, handlers);
          this.finishStream();
        })
        .catch((error) => {
          if (error && error.name === "AbortError") {
            // Explicit swallow (#207 AC 1.5): an AbortError here ALWAYS
            // means the stream was ended on purpose, client-side —
            // resetChat() or the composer's Stop control (stopStreaming)
            // called finishStream(), which aborts this fetch only AFTER
            // the stream state is finalized (partial assistant message
            // kept, phase idled, watchdog cleared). The rejection is the
            // transport echo of that deliberate local abort: swallowing it
            // is the contract — no error banner, no state write.
            return;
          }
          if (epoch === this.streamEpoch) {
            if (this.streaming) {
              // Transport-error path (#84) — separate from the typed agent
              // error handler in streamHandlers: this one finishes the
              // stream, because no done event will follow a dead transport.
              // Surface the underlying transport error message when the
              // browser provides one (e.g. "Stream unavailable (500)" or
              // "Failed to fetch"), falling back to the generic "Connection
              // failed" string; the assistant bubble prefixes it with ⚠️ and
              // the banner keeps the failure visible after finishStream
              // clears the status line (AC5: no silent error path).
              this.phase = "error";
              this.statusText = error.message || "Connection failed";
              this.errorBanner =
                error.message || "Connection failed. You can send another message.";
              // 401 sign-in action (#180): an expired or missing session
              // surfaces a real /auth/login link next to the banner message
              // (defense-in-depth for the server-injected identity-bar
              // link). Any other failure keeps the action hidden. The
              // banner stays non-blocking (#84): the composer is untouched.
              this.errorBannerSignin = error.status === 401;
              if (!this.messages[index].content) {
                this.messages[index].content = "⚠️ " + this.statusText;
              }
            }
            this.finishStream();
          }
        });
    },

    async consumeSse(body, handlers) {
      // Minimal SSE frame parser over a fetch ReadableStream. sse-starlette
      // terminates lines with \r\n, so the pending buffer is normalised to
      // \n before frames (delimited by a blank line) are split off. A \r\n
      // split across chunk boundaries is handled because the whole pending
      // buffer is re-normalised on every read before any frame is
      // dispatched. Ping comment frames (": ping …") carry no event/data
      // lines and are skipped by dispatchSseFrame.
      const reader = body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");
        let boundary = buffer.indexOf("\n\n");
        while (boundary !== -1) {
          this.dispatchSseFrame(buffer.slice(0, boundary), handlers);
          buffer = buffer.slice(boundary + 2);
          boundary = buffer.indexOf("\n\n");
        }
      }
      const tail = (buffer + decoder.decode()).replace(/\r\n/g, "\n").trim();
      if (tail) {
        this.dispatchSseFrame(tail, handlers);
      }
    },

    dispatchSseFrame(frame, handlers) {
      let type = "";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) {
          type = line.slice("event:".length).trim();
        } else if (line.startsWith("data:")) {
          data += (data ? "\n" : "") + line.slice("data:".length).trim();
        }
      }
      if (type) {
        // Any typed event — known or not — proves the agent is alive:
        // restart the 90 s no-event watchdog (AC3). Ping comment frames
        // carry no event line and deliberately do NOT reset it, so a wedged
        // agent behind a live transport still trips the watchdog.
        this.resetWatchdog();
      }
      if (type && Object.prototype.hasOwnProperty.call(handlers, type)) {
        // Same payload shape as a native SSE event object, so parsePayload
        // and every handler stay transport-agnostic.
        handlers[type]({ data: data });
      }
    },

    streamHandlers(index, epoch) {
      return {
        status: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
          this.statusText = data.phase === "thinking" ? "Thinking" : (data.phase || "");
          if (data.phase === "thinking") {
            // A new agent iteration is thinking (e.g. after a tool round):
            // back to the waiting dots until the next token arrives (AC1).
            this.phase = "waiting";
          }
        },

        thought: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
          if (data.text) {
            this.thoughts.push(data.text);
          }
        },

        token: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
          if (data.text) {
            // First token ends the waiting dots (AC1: streaming is distinct).
            this.phase = "streaming";
            this.messages[index].content += data.text;
            this.scrollToBottom();
          }
        },

        tool_call: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
          this.phase = "tooling"; // AC1: MCP tools running
          // #199: the friendly static label — raw tool names never render.
          this.statusText = "Coach is checking your training data…";
          // In-flight tracking (#199): the call id joins the tracker; the
          // indicator clears when tool_result releases the last id. The
          // raw tool name is NOT stored — internal tool names never
          // surface (nLPD/#142 posture). Unidentifiable calls are not
          // tracked (they could never be released).
          if (data.id && !this.tools.includes(data.id)) {
            this.tools.push(data.id);
          }
        },

        tool_start: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          this.phase = "tooling"; // AC1: MCP tools running
          // #199: the friendly static label — raw tool names never render.
          this.statusText = "Coach is checking your training data…";
        },

        tool_result: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
          // Release the call (#199): the id leaves the tracker; when the
          // last in-flight call finishes, the indicator clears and the
          // phase returns to the waiting dots — the coach reasons over
          // the results next (AC1: no lingering tool chatter).
          this.tools = this.tools.filter((id) => id !== data.id);
          if (this.tools.length === 0) {
            this.phase = "waiting";
            this.statusText = "Thinking";
          } else {
            this.phase = "tooling"; // AC1: tool events keep the tooling sub-state
            this.statusText = "Coach is checking your training data…";
          }
        },

        plan_proposal: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
          this.plan = data.plan || data;
          // Pin the card to the message that proposed it (#82) and follow
          // the log if armed, so the card is visible when it appears.
          this.planMessageIndex = index;
          this.scrollToBottom();
        },

        plan: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
          this.plan = data.plan || data;
          // Same pinning as plan_proposal (#82): one active card, beneath
          // the message that proposed it.
          this.planMessageIndex = index;
          this.scrollToBottom();
        },

        week_plan: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
          // Weekly plan card (#168): the validated draft arrives as
          // data.draft; the card replaces any previous week card (#82
          // rule) and every day chip starts as "proposed".
          this.weekPlan = data.draft || null;
          this.weekMessageIndex = index;
          this.weekApproval = { state: "idle", message: "" };
          const statuses = {};
          (this.weekPlan && this.weekPlan.days ? this.weekPlan.days : []).forEach(
            (day) => {
              statuses[day.date] = "proposed";
            }
          );
          this.weekDayStatus = statuses;
          this.scrollToBottom();
        },

        periodization_plan: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
          // Periodization card (#182): the validated plan arrives as
          // data.plan with the coach's rationale alongside; the card
          // replaces any previous periodization card (#82 rule). BOTH are
          // consumed into state — storing only the plan would silently
          // drop the rationale line the card renders (PR #184 review).
          this.periodizationPlan = data.plan
            ? { plan: data.plan, rationale: data.rationale || "" }
            : null;
          this.periodizationMessageIndex = index;
          this.periodizationApproval = { state: "idle", message: "" };
          this.scrollToBottom();
        },

        rpe_request: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
          // Check-in card (#189): the coach opened the debrief for one
          // session; the card replaces any previous check-in card (#82
          // rule) and the stepper starts at the midpoint value. The
          // payload is session metadata only (key + title) — debrief
          // notes and RPE values never travel through the agent loop.
          this.rpe = data.checkin || null;
          this.rpeMessageIndex = index;
          this.rpeApproval = { state: "idle", message: "" };
          this.rpeValue = 5;
          this.rpeNotes = "";
          // Mirror consent (#190 AC3): strictly opt-in — every newly opened
          // check-in starts with the consent OFF.
          this.mirrorToIcu = false;
          this.scrollToBottom();
        },

        debrief_summary: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
          // Debrief summary card (#190): the server built the card from the
          // fetched ICU compliance data — the client only renders it
          // (structured x-text, #165). A new card replaces the previous
          // one (#82 rule).
          this.debriefSummary = data.summary || null;
          this.debriefSummaryMessageIndex = index;
          this.scrollToBottom();
        },

        morning_briefing: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
          // Morning briefing card (#262): the server derived the card
          // statelessly (readiness band + today's session + cues) — the
          // client only renders it (structured x-text, ADR-006 §9). A new
          // card replaces the previous one (#82 rule).
          this.morningBriefing = data.briefing || null;
          this.morningBriefingMessageIndex = index;
          this.scrollToBottom();
        },

        error: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
          // Typed agent error path (#84, AC2) — separate from the
          // transport-error catch in startStream. agent.py emits a typed
          // error FOLLOWED BY done on its OpenRouterError path, so this
          // handler must NOT end the stream: ending it here would flip the
          // compound guards and truncate the follow-up done. The done
          // handler ends the stream; the banner keeps the failure visible
          // after the status line is cleared (AC5).
          this.phase = "error";
          this.statusText = data.message || "Error";
          this.errorBanner =
            data.message || "The coach hit an error. You can send another message.";
          // A typed agent error is never a 401 (the middleware rejects those
          // before the stream exists) — keep the sign-in action off (#180).
          this.errorBannerSignin = false;
          if (!this.messages[index].content) {
            this.messages[index].content = "⚠️ " + this.statusText;
          }
        },

        done: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
          if (!this.messages[index].content && data.message) {
            this.messages[index].content = data.message;
          }
          // Quick-reply pills (#281, MADR-0011): the terminal done event may
          // carry the optional server-owned suggestions array. The shape
          // guard renders only from a valid payload — an absent or
          // suppressed field renders nothing (C-5d). The pills pin to this
          // message (#82 grammar); the state is volatile client memory only
          // (C-6) and is cleared when the next send/stream starts.
          this.suggestions = normalizeSuggestions(data.suggestions);
          this.suggestionsMessageIndex = this.suggestions.length > 0 ? index : -1;
          // Announce the completed FINAL message (AC3, #86): the full
          // text, once — never per token. Markdown-stripped (#149 AC2):
          // the status region speaks plain prose — raw markdown source
          // ("**bold**", list markers) reads as syntax noise. The visual
          // x-html path (renderMarkdown → marked + DOMPurify) is untouched.
          if (this.messages[index].content) {
            this.announce(stripMarkdown(this.messages[index].content));
          }
          this.finishStream();
        },
      };
    },

    armWatchdog(epoch, index) {
      // Arm (or re-arm) the 90 s no-event watchdog for this stream (AC3).
      // Defensive clear first: a new stream must never inherit a pending
      // timer from a superseded one.
      if (this.watchdog !== null) {
        clearTimeout(this.watchdog);
      }
      this.watchdogEpoch = epoch;
      this.watchdogIndex = index;
      this.watchdog = setTimeout(() => this.handleWatchdogTimeout(), WATCHDOG_TIMEOUT_MS);
    },

    resetWatchdog() {
      // Restart the watchdog on every received typed event (dispatchSseFrame).
      // A no-op once the stream ended — finishStream cleared the timer — so
      // late frames cannot resurrect it.
      if (this.watchdog !== null) {
        clearTimeout(this.watchdog);
        this.watchdog = setTimeout(() => this.handleWatchdogTimeout(), WATCHDOG_TIMEOUT_MS);
      }
    },

    clearWatchdog() {
      if (this.watchdog !== null) {
        clearTimeout(this.watchdog);
        this.watchdog = null;
      }
    },

    handleWatchdogTimeout() {
      this.watchdog = null;
      if (!this.streaming || this.watchdogEpoch !== this.streamEpoch) {
        return; // stale timer from an ended or superseded stream
      }
      // No agent event for 90 s (AC3): surface a visible, non-blocking
      // error and release the composer — the user can retry by sending
      // again, no reload. Banner text is static: no message content (nLPD).
      this.phase = "error";
      this.statusText = "Timed out";
      this.errorBanner =
        "The coach stopped responding (no updates for 90 s). You can send another message.";
      // A timeout is not a 401 — keep the sign-in action off (#180).
      this.errorBannerSignin = false;
      if (!this.messages[this.watchdogIndex].content) {
        this.messages[this.watchdogIndex].content = "⚠️ " + this.statusText;
      }
      this.finishStream();
    },

    finishStream() {
      this.streaming = false;
      this.statusText = "";
      this.phase = "idle";
      // In-flight tool tracker (#199): a finished or failed stream can
      // never leave a lingering activity indicator behind.
      this.tools = [];
      this.clearWatchdog();
      if (this.controller) {
        this.controller.abort();
        this.controller = null;
      }
      // Return focus to the composer (AC4, #86): disabling the textarea
      // on stream start drops focus to <body>; when the stream ends,
      // keyboard users get the composer back without re-tabbing. Focus
      // is never stolen from a deliberate target (e.g. a plan-card
      // button tabbed to while streaming).
      const active = document.activeElement;
      if ((!active || active === document.body) && this.$refs.composer) {
        this.$refs.composer.focus();
      }
    },

    resetChat() {
      this.finishStream();
      this.messages = [];
      this.thoughts = [];
      this.tools = [];
      this.plan = null;
      this.planMessageIndex = -1;
      this.approval = { state: "idle", message: "" };
      this.weekPlan = null;
      this.weekMessageIndex = -1;
      this.weekApproval = { state: "idle", message: "" };
      this.weekDayStatus = {};
      this.periodizationPlan = null;
      this.periodizationMessageIndex = -1;
      this.periodizationApproval = { state: "idle", message: "" };
      // Check-in card (#189): a chat reset clears the debrief state — the
      // pending banner is page-level state and survives (MADR-0010).
      this.rpe = null;
      this.rpeMessageIndex = -1;
      this.rpeApproval = { state: "idle", message: "" };
      this.rpeValue = 5;
      this.rpeNotes = "";
      // Debrief summary card + mirror consent (#190): a chat reset clears
      // both — the pending banner is page-level state and survives.
      this.debriefSummary = null;
      this.debriefSummaryMessageIndex = -1;
      this.mirrorToIcu = false;
      // Morning briefing card (#262): a chat reset clears the card; the
      // date flag is request-time state and is NOT reset (the briefing
      // was already requested today).
      this.morningBriefing = null;
      this.morningBriefingMessageIndex = -1;
      // Quick-reply pills (#281, C-5d/C-6): a chat reset clears the pill
      // state — volatile client memory only.
      this.suggestions = [];
      this.suggestionsMessageIndex = -1;
      this.input = "";
      this.errorBanner = "";
      this.errorBannerSignin = false;
      this.liveAnnouncement = "";
      this.phase = "idle";
      // Re-arm following (#85): the emptied log cannot fire a scroll event
      // (nothing left to scroll), so a fresh chat re-arms explicitly —
      // otherwise a follow paused on the old transcript would leak into
      // the new one (AC2).
      this.stickToBottom = true;
    },

    approvePlan() {
      if (!this.plan || this.approval.state === "submitting") {
        return;
      }
      this.approval = { state: "submitting", message: "Pushing plan…" };

      fetch("/api/plan/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan: this.plan }),
      })
        .then(async (response) => {
          const body = await response.json().catch(() => ({}));
          if (!response.ok) {
            throw new Error(body.detail || "Approval failed");
          }
          if (body.status === "approved") {
            this.approval = {
              state: "approved",
              message: "Plan approved and pushed.",
            };
          } else {
            this.approval = {
              state: "error",
              message: "Plan partially pushed. Review the server logs.",
            };
          }
        })
        .catch((error) => {
          this.approval = { state: "error", message: error.message };
        });
    },

    rejectPlan() {
      // Dismiss the card and clear plan state (#82 AC2): the pin goes with
      // it so no stale index survives an approved/rejected proposal.
      this.plan = null;
      this.planMessageIndex = -1;
      this.approval = { state: "idle", message: "" };
    },

    // ------------------------------------------------------------------
    // Weekly plan card (#168). The proposed 7-day microcycle is approved
    // as one POST (/api/week/approve) that writes one Intervals.icu event
    // per day — NOT atomic (#163): every day reports its own outcome, a
    // partial failure stays retryable via the failed-days subset, and the
    // card state mirrors the plan card's plain-string machine. Adjust
    // prefills the composer and NEVER auto-sends (PO Q8).
    // ------------------------------------------------------------------

    setDayStatus(date, status) {
      // Immutable update: reassign the map so Alpine's reactivity sees the
      // per-day chip change (word + color, never color alone — #165).
      this.weekDayStatus = Object.assign({}, this.weekDayStatus, { [date]: status });
    },

    weekDayStatusText(day) {
      // Chip word per state (#165): proposed → pushing… → approved ✓ /
      // write failed. Unknown days default to "proposed".
      return WEEK_DAY_STATUS_WORDS[this.weekDayStatus[day.date] || "proposed"];
    },

    weekDayChipClass(day) {
      // Chip color class per state — always rendered NEXT TO the word.
      return "day-chip--" + (this.weekDayStatus[day.date] || "proposed");
    },

    weekDayLabel(day) {
      // Compact day label, e.g. "Mon 06 Oct" (#165 wireframe). Built from
      // the ISO parts — no Date parsing, no timezone drift.
      const parts = day.date.split("-");
      const month = MONTH_LABELS[Number(parts[1]) - 1] || "";
      return (WEEK_DAY_LABELS[day.day_of_week] || "") + " " + parts[2] + " " + month;
    },

    weekDayFocus(day) {
      // Focus/zones cell: zones joined, falling back to the focus line.
      if (day.rest_day) {
        return "—";
      }
      const zones = (day.zones || []).join("–");
      return zones || day.focus || "—";
    },

    weekDayIntensityText(day) {
      // Per-day intensity badge (#235 AC4): "Rest" for rest days, "Hard"
      // when the day's delivered zones include Z4 or harder, "Easy"
      // otherwise. Pure function of the delivered week_plan payload — no
      // new I/O (AC2). The word carries the meaning; the color class
      // returned by weekDayIntensityClass() only reinforces it (#165:
      // never color alone).
      if (day.rest_day) {
        return "Rest";
      }
      const hard = (day.zones || []).some((zone) => /^Z[4-9]$/i.test(zone));
      return hard ? "Hard" : "Easy";
    },

    weekDayIntensityClass(day) {
      // Chip color class per intensity — always rendered NEXT TO the word,
      // derived from the same text the chip renders (never color alone).
      return "zone-chip--" + this.weekDayIntensityText(day).toLowerCase();
    },

    get weekSummaryLine() {
      // Week summary (#165): "520 TSS · 2 hard · 4 easy · 1 rest". The
      // rest count is derived from the day slots (the ground truth).
      if (!this.weekPlan) {
        return "";
      }
      const rest = this.weekPlan.days.filter((day) => day.rest_day).length;
      return (
        this.weekPlan.total_tss +
        " TSS · " +
        this.weekPlan.hard_days +
        " hard · " +
        this.weekPlan.easy_days +
        " easy · " +
        rest +
        " rest"
      );
    },

    approveWeek(dates) {
      // Approve All (dates = null) or retry the failed-days subset. Target
      // days flip to "pushing" synchronously — before the fetch — and a
      // failed request returns them to "proposed" so no day is ever stuck.
      if (!this.weekPlan || this.weekApproval.state === "submitting") {
        return;
      }
      const targetDays = this.weekPlan.days.filter(
        (day) => !dates || dates.indexOf(day.date) !== -1
      );
      if (targetDays.length === 0) {
        return;
      }
      targetDays.forEach((day) => this.setDayStatus(day.date, "pushing"));
      this.weekApproval = {
        state: "submitting",
        message: dates ? "Retrying failed days…" : "Pushing weekly plan…",
      };

      fetch("/api/week/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ week: this.weekPlan, dates: dates || null }),
      })
        .then(async (response) => {
          const body = await response.json().catch(() => ({}));
          if (!response.ok) {
            throw new Error(body.detail || "Approval failed");
          }
          let failed = 0;
          body.results.forEach((result) => {
            this.setDayStatus(result.date, result.success ? "approved" : "failed");
            if (!result.success) {
              failed += 1;
            }
          });
          if (failed === 0) {
            this.weekApproval = {
              state: "approved",
              message: "Weekly plan approved and pushed.",
            };
          } else {
            this.weekApproval = {
              state: "partially_failed",
              message:
                failed + " of " + body.results.length + " days failed to write. Retry the failed days.",
            };
          }
        })
        .catch((error) => {
          targetDays.forEach((day) => {
            if (this.weekDayStatus[day.date] === "pushing") {
              this.setDayStatus(day.date, "proposed");
            }
          });
          this.weekApproval = { state: "error", message: error.message };
        });
    },

    retryFailedDays() {
      // Retry affordance (#163): post ONLY the failed dates subset —
      // succeeded days are never re-written (no duplicate calendar events).
      if (!this.weekPlan) {
        return;
      }
      const failedDates = this.weekPlan.days
        .map((day) => day.date)
        .filter((date) => this.weekDayStatus[date] === "failed");
      this.approveWeek(failedDates);
    },

    adjustDay(day) {
      // Per-day Adjust (PO Q8): prefill-and-focus — NEVER auto-send. The
      // agent's next turn emits a fresh week_plan that replaces the card
      // (#82 rule), keeping one approval contract.
      this.input = "Adjust " + (WEEK_DAY_FULL_NAMES[day.day_of_week] || "that") + "'s session: ";
      this.$refs.composer.focus();
    },

    rejectWeek() {
      // Dismiss the weekly card and clear every week state slice.
      this.weekPlan = null;
      this.weekMessageIndex = -1;
      this.weekApproval = { state: "idle", message: "" };
      this.weekDayStatus = {};
    },

    // ------------------------------------------------------------------
    // Periodization card (#182). The coach-proposed season phase plan is
    // approved as one POST (/api/periodization/approve) — the server
    // re-validates the plan and persists it through the #167 repository
    // (coverage-validated). The explicit approval is the human-in-the-loop
    // gate (#87 C7): no phase persistence without the athlete's consent.
    // Reject dismisses the card without persisting.
    // ------------------------------------------------------------------

    approvePeriodization() {
      if (!this.periodizationPlan || this.periodizationApproval.state === "submitting") {
        return;
      }
      this.periodizationApproval = { state: "submitting", message: "Saving phases…" };

      fetch("/api/periodization/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan: this.periodizationPlan.plan }),
      })
        .then(async (response) => {
          if (!response.ok) {
            // 401 (#181 AC2): an expired or missing session is a sign-in
            // problem, not a plan problem — never "try again".
            if (response.status === 401) {
              this.periodizationApproval = {
                state: "error",
                message: "Please sign in.",
              };
              return;
            }
            // 422 (#181 AC1): the owner-safe structured reason body
            // {code, message} renders for known codes (the live coverage
            // case); unknown/missing codes fall back to the generic text
            // (fail-safe). The generic detail body is never read and no
            // error text is echoed (nLPD #142 posture for everything else).
            const body = await response.json().catch(() => ({}));
            this.periodizationApproval = {
              state: "error",
              message: settingsReasonMessage(body, "Could not save the phase plan. Try again."),
            };
            return;
          }
          this.periodizationApproval = {
            state: "approved",
            message: "Periodization approved.",
          };
        })
        .catch(() => {
          // Transport failure: the same generic message — no server detail
          // and no error text echoed (nLPD #142 posture).
          this.periodizationApproval = {
            state: "error",
            message: "Could not save the phase plan. Try again.",
          };
        });
    },

    rejectPeriodization() {
      // Dismiss the card and clear the periodization state (#82 AC2): the
      // pin goes with it, and nothing is persisted.
      this.periodizationPlan = null;
      this.periodizationMessageIndex = -1;
      this.periodizationApproval = { state: "idle", message: "" };
    },

    // ------------------------------------------------------------------
    // Debrief capture (#189). The coach's rpe_request event opens the
    // session check-in card; LOG CHECK-IN posts {plan_session_key, rpe,
    // notes} to POST /api/debrief — the server encrypts the notes (C2)
    // and persists per athlete. The card collapses to the one-line
    // summary carried by the role="status" note; Skip dismisses without
    // persisting. The pending-debrief banner (MADR-0010) is fetched once
    // at init and stays passive: LOG IT prefills the composer (never
    // auto-send, PO Q8), × dismisses client-only.
    // ------------------------------------------------------------------

    stepRpe(delta) {
      // Stepper (#165): two real buttons clamping the value into 1..10 —
      // no range input, no drag, keyboard-operable by construction.
      if (!this.rpe || this.rpeApproval.state === "submitted") {
        return;
      }
      this.rpeValue = Math.min(10, Math.max(1, this.rpeValue + delta));
    },

    logRpe() {
      if (
        !this.rpe ||
        this.rpeApproval.state === "submitting" ||
        this.rpeApproval.state === "submitted"
      ) {
        return;
      }
      this.rpeApproval = { state: "submitting", message: "Saving check-in…" };

      fetch("/api/debrief", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          plan_session_key: this.rpe.plan_session_key,
          rpe: this.rpeValue,
          notes: this.rpeNotes.trim() === "" ? null : this.rpeNotes,
          mirror_to_icu: this.mirrorToIcu === true,
        }),
      })
        .then(async (response) => {
          if (!response.ok) {
            // 401 (#180): an expired or missing session is a sign-in
            // problem, not a check-in problem — never "try again".
            if (response.status === 401) {
              this.rpeApproval = { state: "error", message: "Please sign in." };
              return;
            }
            // 503 (#189 AC2): the fixed encryption vocabulary renders for
            // the known code; unknown codes and other failures keep the
            // generic text. No error text is echoed (nLPD #142 posture).
            const body = await response.json().catch(() => ({}));
            this.rpeApproval = {
              state: "error",
              message: debriefReasonMessage(body, "Could not save the check-in. Try again."),
            };
            return;
          }
          // Collapsed summary (#165 §4.2): the card's editing controls
          // hide and the role="status" note carries the one-line
          // confirmation — metadata only, the notes are never echoed.
          // The ICU mirror outcome (#190 AC3) rides the same note: a
          // successful mirror appends its confirmation, a failed mirror
          // appends the server's fixed soft warning — the debrief stays
          // saved either way (the message leads with the saved line).
          const body = await response.json().catch(() => ({}));
          let note = "Logged: RPE " + this.rpeValue + " · " + this.rpe.plan_session_key;
          // Weekly adherence (#234 AC3): the save response carries the
          // debriefed session's ISO-week metric (counts + percentage, C3) —
          // rendered on this same card's collapsed summary, no new fetch.
          if (body.week_adherence) {
            note +=
              " · Week adherence " +
              body.week_adherence.adherence_pct +
              "% (" +
              body.week_adherence.completed_count +
              "/" +
              body.week_adherence.planned_count +
              ")";
          }
          if (body.mirror && body.mirror.status === "mirrored") {
            note += " · Mirrored to Intervals.icu.";
          } else if (body.mirror && body.mirror.status === "failed") {
            note += " · " + body.mirror.message;
          }
          this.rpeApproval = {
            state: "submitted",
            message: note,
          };
        })
        .catch(() => {
          // Transport failure: the same generic message — no server detail
          // and no error text echoed (nLPD #142 posture).
          this.rpeApproval = {
            state: "error",
            message: "Could not save the check-in. Try again.",
          };
        });
    },

    skipRpe() {
      // Dismiss the check-in card and clear every debrief slice: nothing
      // is persisted (the session stays pending for a later greeting).
      this.rpe = null;
      this.rpeMessageIndex = -1;
      this.rpeApproval = { state: "idle", message: "" };
      this.rpeValue = 5;
      this.rpeNotes = "";
      this.mirrorToIcu = false;
    },

    severityWord(severity) {
      // Severity chip word (#190, #165): static vocabulary for the
      // server-assigned token — word + tokenized color, never color alone.
      return DEBRIEF_SEVERITY_WORDS[severity] || severity;
    },

    loadPendingDebriefs() {
      // Pending-debrief banner (#189 AC3, MADR-0010): fetched once at
      // init. Passive by contract — a 401 (not signed in yet), a server
      // failure or a transport error all degrade to no banner, never to
      // an error surface (MADR-0010: degrade to silence).
      fetch("/api/debrief/pending")
        .then(async (response) => {
          if (!response.ok) {
            return;
          }
          const body = await response.json();
          this.pendingDebriefs = body.pending || [];
        })
        .catch(() => {
          // Passive reminder: silence is the failure mode.
        });
    },

    prefillDebrief(item) {
      // LOG IT (PO Q8): prefill-and-focus — NEVER auto-send. The coach's
      // next turn opens the check-in card via rpe_request.
      this.input = "How did " + item.session_title + " on " + item.plan_session_key + " feel? ";
      this.$refs.composer.focus();
    },

    dismissPendingDebrief(key) {
      // Client-only dismissal (#165 component 4): the reminder reappears
      // on the next page load — acceptable for a passive reminder (KIS;
      // no dismissal persistence in 3.1).
      this.pendingDebriefs = this.pendingDebriefs.filter(
        (item) => item.plan_session_key !== key
      );
    },

    stepProfileRows(plan) {
      // Step-profile geometry (#235 AC1): one row per step — the bar
      // height percent (0–100) proportional to the step's target
      // intensity, plus the target/cadence label texts for the
      // x-text labels beside each bar. Pure function of the delivered
      // payload — no new I/O (AC2).
      //
      // Intensity prefers % of FTP (the bounded 0–250 domain; a step
      // carrying both watts and % maps by its %) and falls back to raw
      // watts. Each bar is normalized against the plan's peak intensity;
      // a step without any power target renders a neutral
      // STEP_PROFILE_STUB_PCT stub instead of inventing a value.
      if (!plan || !plan.steps) {
        return [];
      }
      const values = plan.steps.map((step) =>
        step.target_power_pct != null
          ? step.target_power_pct
          : step.target_power_watts != null
            ? step.target_power_watts
            : null
      );
      const peak = values.reduce(
        (max, value) => (value != null && value > max ? value : max),
        0
      );
      return plan.steps.map((step, stepIndex) => {
        const value = values[stepIndex];
        const height =
          value == null || peak === 0
            ? STEP_PROFILE_STUB_PCT
            : Math.max(Math.round((value / peak) * 100), 1);
        return {
          height: height,
          target: this.stepTargetText(step),
          cadence: step.cadence_rpm ? step.cadence_rpm + " rpm" : "",
        };
      });
    },

    stepTargetText(step) {
      // Target label mirroring the server's plan_approval._format_target
      // vocabulary (#235): watts preferred, with the % of FTP alongside
      // when the step carries both.
      if (step.target_power_watts != null) {
        return step.target_power_pct != null
          ? step.target_power_watts + " W (" + step.target_power_pct + "%)"
          : step.target_power_watts + " W";
      }
      if (step.target_power_pct != null) {
        return step.target_power_pct + "%";
      }
      return "";
    },

    stepProfileLabel(plan) {
      // Accessible name for the step-profile image (#235 AC3): a SUMMARY —
      // step count, total duration and the peak target rendered with the
      // same stepTargetText() vocabulary as the visible bar labels — never
      // a per-step repeat, because the interval table below the profile
      // remains the accessible per-step alternative (AC3).
      if (!plan || !plan.steps || plan.steps.length === 0) {
        return "";
      }
      let total = 0;
      let peakValue = 0;
      let peakText = "";
      plan.steps.forEach((step) => {
        total += step.duration_minutes;
        const value =
          step.target_power_pct != null
            ? step.target_power_pct
            : step.target_power_watts != null
              ? step.target_power_watts
              : null;
        if (value != null && value > peakValue) {
          peakValue = value;
          peakText = " · peak " + this.stepTargetText(step);
        }
      });
      return (
        "Step profile: " +
        plan.steps.length +
        " steps, " +
        total +
        " min" +
        peakText
      );
    },

    get planDuration() {
      if (!this.plan || !this.plan.steps) {
        return 0;
      }
      return this.plan.steps.reduce(
        (total, step) => total + step.duration_minutes,
        0
      );
    },
  };
}

if (typeof document !== "undefined") {
  document.addEventListener("alpine:init", () => {
    if (window.Alpine) {
      window.Alpine.data("coachApp", coachApp);
    }
  });
  window.coachApp = coachApp;
}
