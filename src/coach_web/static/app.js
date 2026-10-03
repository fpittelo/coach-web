/* Coach Web — Alpine.js chat console and plan approval controller.
 *
 * Consumes the typed SSE stream exposed by POST /api/agent/stream through
 * fetch + ReadableStream (the native SSE browser API cannot POST) and posts
 * approved plans to POST /api/plan/approve. The conversation is client-owned:
 * every turn replays the last ≤ 10 transcript messages as history inside the
 * request body — message and history content never appear in a URL or query
 * string (#79, Swiss nLPD). "New chat" (resetChat) is a full client-side
 * reset: it aborts any in-flight stream and clears every state slice without
 * transmitting anything. User data is only ever bound with x-text; no raw
 * HTML is injected.
 */

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
    statusText: "",
    thoughts: [],
    tools: [],
    plan: null,
    approval: { state: "idle", message: "" },
    controller: null,

    init() {
      this.$watch("messages", () => this.scrollToBottom());
    },

    scrollToBottom() {
      this.$nextTick(() => {
        const log = this.$refs.log;
        if (log) {
          log.scrollTop = log.scrollHeight;
        }
      });
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
      this.statusText = "Connecting";
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
              // Surface the underlying transport error message when the
              // browser provides one (e.g. "Stream unavailable (500)" or
              // "Failed to fetch"), falling back to the generic "Connection
              // failed" string; either way the assistant bubble prefixes it
              // with ⚠️.
              this.statusText = error.message || "Connection failed";
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
            this.messages[index].content += data.text;
            this.scrollToBottom();
          }
        },

        tool_call: (event) => {
          if (!this.streaming || epoch !== this.streamEpoch) {
            return; // stale event from an ended or superseded stream
          }
          const data = parsePayload(event);
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
          this.statusText = data.message || "Error";
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

    finishStream() {
      this.streaming = false;
      this.statusText = "";
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
