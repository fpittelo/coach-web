# Architecture Decision Records (MADR) — Coach Web

Single index for the MADRs owned by this repository (created via the `madr-adr` skill; format and header schema defined there).

**Portfolio-level decisions are not duplicated here.** They live in the HOME portfolio index — `opencode-home-config/docs/adr/README.md` — and apply to this repository (trunk+tag MADR-0017, release publishing MADR-0020, MCP topology MADR-0022, agent roster MADR-0021/0023, security routing MADR-0008).

**Numbering:** one gap-free sequence across the portfolio, so gaps in this table are expected. MADR-0001–0007 remain sections of `docs/architecture.md`; MADR-0009 is the MADR appendix of `docs/e2e-checklist.md`; standalone files start at 0008.

| # | Title | Status | Date |
| :--- | :--- | :--- | :--- |
| [0008 — Persist Coaching Lifecycle Data in SQLite (WAL) with App-Level Field Encryption](0008-persistence-data-classification.md) | C2 health data at rest, app-level Fernet field encryption, per-lane key escrow (C1–C6 embedded) | accepted | 2026-10-10 |
| [0010 — Proactivity Model — Session-Greeting Pull + Passive Pending-Debrief Banner](0010-proactivity-model.md) | Pull-based proactivity; dismissal flags in the persistence layer | accepted | 2026-10-10 |
| [0011 — Quick-Reply Suggestions — Server-Owned Outcome Mapping on the `done` SSE Event](0011-quick-reply-suggestions.md) | Suggestions ride the existing `done` frame; server-owned outcome mapping (C-1…C-7) | accepted | 2026-10-18 |
| [0024 — Always-On Prod Lane with Release-Verified Auto-Update](0024-always-on-prod-lane-release-verified-auto-update.md) | systemd user units + provenance-gated 5-min digest update; supersedes the manual prod rollout in ADR-007 §10 | accepted | 2026-10-25 |
