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
    approval: { state: "idle", message: "" },
    controller: null,

    init() {
      this.$watch("messages", () => this.scrollToBottom());
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
        },

        plan: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
          this.plan = data.plan || data;
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
    },

    resetChat() {
      this.finishStream();
      this.messages = [];
      this.thoughts = [];
      this.tools = [];
      this.plan = null;
      this.approval = { state: "idle", message: "" };
      this.input = "";
      this.errorBanner = "";
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
      this.plan = null;
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
