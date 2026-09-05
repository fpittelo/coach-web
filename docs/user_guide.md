# 📗 User Guide — Coach Web

**Audience:** @fpittelo (Athlete & Product Owner)  
**Last updated:** 2026-09-05

---

## 🚀 Getting Started

### What You Need

1. **The Coach MCP server running** — either locally or on a remote host
2. **A GitHub Personal Access Token** — with `repo:read` scope (for fetching training plans)
3. **Your browser** — that's it, no plugins, no extensions

### First Launch

```bash
# Make sure the Coach MCP server is running
# (in the coach repo):
# docker run -d --rm -p 8000:8000 -e MCP_TRANSPORT=streamable_http -e INTERVALS_API_KEY=xxx ghcr.io/fpittelo/coach:latest

# Start Coach Web
streamlit run src/coach_web/app.py --server.port 8501
```

Open [http://localhost:8501](http://localhost:8501) in your browser.

---

## 📊 Dashboard — The Readiness Cockpit

The main dashboard shows your live readiness metrics from Intervals.icu, fetched through the Coach MCP server.

### Metric Cards

| Card | What it means | Good range |
|:---|:---|:---|
| **FTP** | Your current cycling Functional Threshold Power | Your baseline (e.g., 226 W) |
| **Resting HR** | Morning resting heart rate | Lower = better recovery |
| **HRV (rMSSD)** | Heart rate variability | Higher = more ready |
| **Sleep** | Last night's sleep duration | 7.5–9.0 h |
| **CTL (Fitness)** | 42-day chronic training load | Trending up over time |
| **ATL (Fatigue)** | 7-day acute training load | Watch for spikes |
| **TSB (Form)** | CTL minus ATL | -10 to +10 = productive |

### Trend Charts

- **42-day fitness trend** (CTL) — shows your aerobic engine growing over time
- **7-day fatigue trend** (ATL) — shows how loaded your current week is
- **Form battery** (TSB) — shows whether you're in a building zone (negative) or tapering zone (positive)

---

## 📋 Weekly Training Plans

The **Plans** tab shows your last 5 training weeks + the current week.

### How It Works

1. The app fetches issues from the `fpittelo/coach` repository with labels `agent::coach` and `type::task`
2. Each issue is a weekly microcycle plan with:
   - Daily sessions (Zwift cycling, kettlebell strength, or rest)
   - Explicit wattage for every interval (HOME Rule #6 — no % FTP)
   - Duration, planned load (TSS), and Intervals.icu event IDs
   - Execution tracking checklists
3. The plans are rendered as native markdown — just like reading them on GitHub, but with a cleaner layout

### Navigating Plans

- **Most recent first** — current week at the top
- **Expandable sections** — click to expand each day's detailed structure
- **Color-coded sessions** — cycling (red), kettlebell (green), rest (gray)

---

## 💬 Chat with Your Coach

The **Chat** tab lets you talk to the Coach MCP server directly through a Streamlit chat interface.

- Type a question (e.g., "What's my current FTP?")
- The app calls the Coach MCP server over the MCP protocol
- The response is rendered in the chat window

---

## 🔄 Refreshing Data

All data is cached for 60 seconds (TTL). To force a refresh:

1. Click **"Rerun"** in the Streamlit top-right menu, or
2. Press **`R`** on your keyboard while focused on the app

---

## 🐳 Docker Deployment

See the [Admin Guide](admin_guide.md) for Docker and CI/CD deployment instructions.

---

## ❓ Troubleshooting

| Problem | Solution |
|:---|:---|
| **Dashboard shows blank cards** | Check that the Coach MCP server is running at `COACH_MCP_URL` |
| **Plans tab is empty** | Verify `GITHUB_TOKEN` has `repo:read` scope and the `coach` repo has issues with `agent::coach` label |
| **Chat doesn't respond** | Ensure the MCP server is running with `MCP_TRANSPORT=streamable_http` |
| **Charts not rendering** | Refresh the page; Plotly requires a modern browser (Chrome, Firefox, Safari, Edge) |

---

_Last updated: 2026-09-05_