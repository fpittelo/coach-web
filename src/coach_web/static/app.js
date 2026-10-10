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
      // Auto-grow composer (AC1, #83): reset to the intrinsic single-row
      // height first so scrollHeight reflects the current content, then
      // size the box to fit it. The border correction reads the computed
      // border widths — scrollbar-blind, unlike a client/offset box
      // delta, which would absorb a horizontal scrollbar's height
      // (~15px) and over-size the box if one ever appeared (#130
      // review). overflow-y: auto only engages past the stylesheet's
      // ~5-line max-height cap — never at the fitted size. Clearing the
      // input (send / reset) shrinks the box back through the same
      // $watch("input") path.
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
      // Enter-to-send (AC2, #83): plain Enter prevents the newline and
      // sends; Shift+Enter fails the !event.shiftKey half and falls
      // through to the textarea's default action (newline inserted). The
      // IME composition guard returns early while an input method editor
      // is composing (e.g. Japanese kana conversion) — the
      // composition-confirm Enter must never send.
      if (event.isComposing) {
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        this.send();
      }
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

    send() {
      const message = this.input.trim();
      if (!message || this.streaming) {
        return;
      }
      this.messages.push({ role: "user", content: message });
      this.input = "";
      this.startStream(message);
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
      // sending again, no reload).
      this.errorBanner = "";
      this.thoughts = [];
      this.tools = [];
      this.plan = null;
      this.planMessageIndex = -1;
      this.approval = { state: "idle", message: "" };

      // History replay (#79): the conversation is client-owned. Snapshot the
      // PRIOR turns before pushing the in-flight assistant placeholder — the
      // just-submitted user message travels as `message` (never duplicated in
      // history) and the empty placeholder is never replayed. The server
      // caps history at 10 entries; mirror the cap here so oversized
      // transcripts are trimmed client-side first.
      const history = this.messages.slice(0, -1).slice(-10);

      this.messages.push({ role: "assistant", content: "" });
      const index = this.messages.length - 1;

      const controller = new AbortController();
      this.controller = controller;
      const handlers = this.streamHandlers(index, epoch);
      // Arm the 90 s no-event watchdog before the fetch dispatches, so a
      // connection that never establishes is covered too (AC3).
      this.armWatchdog(epoch, index);

      fetch("/api/agent/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: message, history: history }),
        signal: controller.signal,
      })
        .then(async (response) => {
          if (!response.ok || !response.body) {
            throw new Error("Stream unavailable (" + response.status + ")");
          }
          await this.consumeSse(response.body, handlers);
          this.finishStream();
        })
        .catch((error) => {
          if (error && error.name === "AbortError") {
            return; // resetChat() aborted this stream on purpose
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
          this.statusText = "Running tools…"; // #148 AC4: visible tooling label
          this.tools.push({
            id: data.id,
            name: data.name,
            state: "running",
          });
        },

        tool_start: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
          this.phase = "tooling"; // AC1: MCP tools running
          this.statusText = "Running tools…"; // #148 AC4: visible tooling label
          const tool = this.tools.find((item) => item.id === data.id);
          if (tool) {
            tool.state = "running";
          }
        },

        tool_result: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
          this.phase = "tooling"; // AC1: tool events keep the tooling sub-state
          this.statusText = "Running tools…"; // #148 AC4: visible tooling label
          const tool = this.tools.find((item) => item.id === data.id);
          if (tool) {
            tool.state = "done";
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
      if (!this.messages[this.watchdogIndex].content) {
        this.messages[this.watchdogIndex].content = "⚠️ " + this.statusText;
      }
      this.finishStream();
    },

    finishStream() {
      this.streaming = false;
      this.statusText = "";
      this.phase = "idle";
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
      this.input = "";
      this.errorBanner = "";
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
