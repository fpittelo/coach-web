/* Coach Web — Alpine.js chat console and plan approval controller.
 *
 * Consumes the typed SSE stream exposed by GET /api/agent/stream through a
 * native EventSource and posts approved plans to POST /api/plan/approve.
 * User data is only ever bound with x-text; no raw HTML is injected.
 */

function coachApp() {
  return {
    messages: [],
    input: "",
    streaming: false,
    statusText: "",
    thoughts: [],
    tools: [],
    plan: null,
    approval: { state: "idle", message: "" },
    source: null,

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
      this.streaming = true;
      this.statusText = "Connecting";
      this.thoughts = [];
      this.tools = [];
      this.plan = null;
      this.approval = { state: "idle", message: "" };

      this.messages.push({ role: "assistant", content: "" });
      const index = this.messages.length - 1;

      const source = new EventSource(
        "/api/agent/stream?message=" + encodeURIComponent(message)
      );
      this.source = source;

      source.addEventListener("status", (event) => {
        const data = JSON.parse(event.data);
        this.statusText = data.phase === "thinking" ? "Thinking" : data.phase;
      });

      source.addEventListener("thought", (event) => {
        this.thoughts.push(JSON.parse(event.data).text);
      });

      source.addEventListener("token", (event) => {
        this.messages[index].content += JSON.parse(event.data).text;
        this.scrollToBottom();
      });

      source.addEventListener("tool_call", (event) => {
        const data = JSON.parse(event.data);
        this.tools.push({
          id: data.id,
          name: data.name,
          state: "running",
        });
      });

      source.addEventListener("tool_start", (event) => {
        const data = JSON.parse(event.data);
        const tool = this.tools.find((item) => item.id === data.id);
        if (tool) {
          tool.state = "running";
        }
      });

      source.addEventListener("tool_result", (event) => {
        const data = JSON.parse(event.data);
        const tool = this.tools.find((item) => item.id === data.id);
        if (tool) {
          tool.state = "done";
        }
      });

      source.addEventListener("plan_proposal", (event) => {
        this.plan = JSON.parse(event.data).plan;
      });

      source.addEventListener("plan", (event) => {
        this.plan = JSON.parse(event.data).plan;
      });

      source.addEventListener("error", (event) => {
        if (event.data) {
          try {
            const err = JSON.parse(event.data);
            this.statusText = err.message || "Error";
            if (!this.messages[index].content) {
              this.messages[index].content = "⚠️ " + this.statusText;
            }
          } catch (error) {
            this.statusText = "Error";
          }
        }
      });

      source.addEventListener("done", () => {
        this.finishStream();
      });

      source.onerror = () => {
        if (this.streaming) {
          this.finishStream();
        }
      };
    },

    finishStream() {
      this.streaming = false;
      this.statusText = "";
      if (this.source) {
        this.source.close();
        this.source = null;
      }
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