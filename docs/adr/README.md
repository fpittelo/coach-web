# Architecture Decision Records (MADR) — Coach Web

Single index for the MADRs owned by this repository (created via the `madr-adr` skill; format and header schema defined there).

**Portfolio-level decisions are not duplicated here.** They live in the HOME portfolio index — `opencode-home-config/docs/adr/README.md` — and apply to this repository (trunk+tag MADR-0017, release publishing MADR-0020, MCP topology MADR-0022, agent roster MADR-0021/0023, security routing MADR-0008).

**Numbering:** numbers are drawn from the single portfolio sequence, so gaps in this table are expected. MADR-0001–0007 remain sections of `docs/architecture.md`; MADR-0009 is the MADR appendix of `docs/e2e-checklist.md`; standalone files start at 0008. The historical records 0008/0010/0011 predate the unified portfolio index and reuse numbers that were later assigned centrally to different decisions — they are kept as-is for link stability (accepted records are never renumbered); every new record takes the next free **portfolio** number (0024 onward).

| # | Title | Status | Date |
| :--- | :--- | :--- | :--- |
| [0008 — Persist Coaching Lifecycle Data in SQLite (WAL) with App-Level Field Encryption](0008-persistence-data-classification.md) | C2 health data at rest, app-level Fernet field encryption, per-lane key escrow (C1–C6 embedded) | accepted | 2026-10-10 |
| [0010 — Proactivity Model — Session-Greeting Pull + Passive Pending-Debrief Banner](0010-proactivity-model.md) | Pull-based proactivity; dismissal flags in the persistence layer | accepted | 2026-10-10 |
| [0011 — Quick-Reply Suggestions — Server-Owned Outcome Mapping on the `done` SSE Event](0011-quick-reply-suggestions.md) | Suggestions ride the existing `done` frame; server-owned outcome mapping (C-1…C-7) | accepted | 2026-10-18 |
| [0024 — Always-On Prod Lane with Release-Verified Auto-Update](0024-always-on-prod-lane-release-verified-auto-update.md) | systemd user units + provenance-gated 5-min digest update; supersedes the manual prod rollout in ADR-007 §10 | accepted | 2026-10-25 |
| [0026 — Admin Orientation and Agent Settings — Prompt Precedence and Sanitizer Contract](0026-admin-orientation-and-agent-settings-prompt-precedence-and-sanitizer-contract.md) | `agent_settings` C1 store + single `assemble_system_prompt` seam; fixed block precedence (core → fenced non-authoritative orientation → pref directives → digests → memory block future → re-assert LAST); two-site sanitizer; memory epic #346 bound by the contract | accepted | 2026-10-25 |
