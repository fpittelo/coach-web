# Architecture Documentation — Coach Web (Redirect Stub)

**Status:** retired monolith — migrated to the HOME **arc42 3-view standard** (#369).

All authoritative architecture documentation now lives under [`docs/architecture/arc42/`](architecture/arc42/README.md):

1. **[Context & Building Blocks](architecture/arc42/01-context-and-building-blocks.md)** — Introduction & Goals, Constraints, System Context (`C4Context`), Solution Strategy, and Building Block View (`C4Container` + ArchiMate 3.x alignment).
2. **[Runtime & Delivery](architecture/arc42/02-runtime-and-delivery.md)** — Runtime sequences (agent chat turn & 7-block prompt seam, Cloud Run encrypted GCS-snapshot rehydration/checkpoint, local MADR-0024 release auto-update & rollback) and Deployment View (Cloud Run `europe-west6` + local Compose lanes).
3. **[Cross-cutting Concerns, Quality & Decisions](architecture/arc42/03-cross-cutting-and-security.md)** — Swiss nLPD C1/C2 encryption & zero-trust controls, CI quality gates, Risks & Technical Debt, Glossary, and the **Pre-MADR Historical Decision Back-Index (`ADR-001`, `ADR-002`, `ADR-006`, `ADR-007`)** (§5).

For standalone Architecture Decision Records (`MADR-0008` onward), see the single authoritative index at [`docs/adr/README.md`](adr/README.md).
