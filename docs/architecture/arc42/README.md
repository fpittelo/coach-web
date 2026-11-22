# arc42 Architecture Documentation — Coach Web

Architecture documentation for **Coach Web** (`fpittelo/coach-web`), following the **arc42** template (v8.0) as operationalized by the `arc42-documentation` skill — consolidated into **3 cohesive views** (#369).

**Document status:** active — 3-view layout (#369, superseding the legacy `docs/architecture.md` monolith). Architecture Decision Records owned by this repository are indexed in the single authoritative index [`docs/adr/README.md`](../../adr/README.md); portfolio-level decisions live in `opencode-home-config/docs/adr/README.md`.

## Index

| View | File | Contents | Status |
| :--- | :--- | :--- | :--- |
| 1. Context & Building Blocks | [01-context-and-building-blocks.md](01-context-and-building-blocks.md) | §1 Introduction & Goals · §2 Architecture Constraints · §3 System Context & Scope (`C4Context`) · §4 Solution Strategy · §5 Building Block View (`C4Container` + ArchiMate 3.x mapping) | consolidated |
| 2. Runtime & Delivery | [02-runtime-and-delivery.md](02-runtime-and-delivery.md) | §1 Runtime View (agent chat turn & 7-block prompt seam, Cloud Run encrypted snapshot rehydration/checkpoint, local MADR-0024 auto-update & rollback) · §2 Deployment View (Cloud Run `europe-west6` + local Compose lanes) | consolidated |
| 3. Cross-cutting & Security | [03-cross-cutting-and-security.md](03-cross-cutting-and-security.md) | §1 Cross-cutting Concepts (Swiss nLPD C1/C2 encryption, zero-trust edge, quality gates) · §2 Quality Requirements · §3 Risks & Technical Debt · §4 Glossary · §5 Architecture Decisions (MADR pointer + pre-MADR back-index) | consolidated |

## Companion Documentation

- **Threat Model & STRIDE Baseline:** [`docs/security.md`](../../security.md)
- **Operations & Cloud Run / Local Lane Runbooks:** [`docs/admin_guide.md`](../../admin_guide.md)
- **Athlete User Guide:** [`docs/user_guide.md`](../../user_guide.md)
- **MADR Index:** [`docs/adr/README.md`](../../adr/README.md)

## Maintenance Rules

- Update triggers per view/section follow the `arc42-documentation` skill §4:
  - System boundary / external actor change → **`01` §3 System Context & Scope**
  - Application component, database, or sidecar change → **`01` §5 Building Block View**
  - API, SSE stream, snapshot, or deployment pipeline flow change → **`02` §1 Runtime View** / **§2 Deployment View**
  - Security, encryption, IAM, or quality-gate policy change → **`03` §1 Cross-cutting Concepts**
  - New architectural decision → [`docs/adr/README.md`](../../adr/README.md)
- An architectural change and its `arc42` update travel in the **same PR**.
- Governance SSOT remains the `home-governance` skill; this documentation describes, it does not govern.
