# MADR-0027: Cloud Run production operating model — always-on scale-to-zero with IAP edge gate

- **Status:** accepted
- **Date:** 2026-11-14
- **Deciders:** @architect, @fpittelo (PO decisions recorded 2026-11-14 during the architect grooming round on epic #313)

## Context

The PO's 2026-10-18 directive (#313, then "Sprint 26") groomed Cloud Run project `coach-web-509209` as an **on-demand deploy/teardown lane** — $0 standing cost, local-first topology as the daily baseline. On 2026-11-14 the PO directed instead: *"deploy the coach-web application in production to Google Cloud Run"* (project `coach-web-509209`), secure, HTTPS, access restricted to the authenticated Google account `frederic.pitteloud@gmail.com` (principal confirmed by the PO the same day).

During the consensus grooming round on 2026-11-14 (#313 amendment), the PO took two decisions that amend the 2026-10-18 grooming:

1. **Operating model:** always-on production deployment, scale-to-zero.
2. **Edge posture:** IAP enabled directly on the Cloud Run service — available without a load balancer since the 2025-04 IAP release (GA), at no added cost — with the in-app whitelist retained.

Existing topology forces: local Docker lanes are the development baseline (ADR-007) and the local **MADR-0024** systemd lane (VIDAR, release-verified auto-update) is the current always-on prod lane; images flow through the GHCR lane mapping of `.github/workflows/deploy.yaml` (issue #90, trunk+tag MADR-0017 — prod images from `v*` tags). Application constraints: 3-container pod (coach-web ingress + coach-mcp + github-mcp sidecars sharing a localhost namespace), SQLite WAL persistence with app-level Fernet field encryption (MADR-0008), in-app fail-closed Google OIDC whitelist, and Swiss nLPD residency.

## Decision Drivers

- **Swiss nLPD / data residency:** all resources pinned `europe-west6` (Zürich).
- **Single-account access at the platform edge:** only `frederic.pitteloud@gmail.com` may reach the service — enforced *before* traffic reaches the application, per the PO's phrasing ("the authenticated google account").
- **Cost discipline for n=1:** idle compute ≈ $0; standing cost rejected for a single-user cockpit.
- **KIS & YAGNI:** gcloud-based operations (no OpenTofu resurrection — #110/#111 precedent); no load balancer, no Cloud Armor.
- **Trunk+tag supply chain (MADR-0017):** production consumes the release-verified GHCR image lane built from `v*` tags.
- **Ephemerality contract:** durable SSOTs are Intervals.icu + GitHub; debrief summaries mirror to ICU (#190) — the cloud database need not be durable.

## Considered Options

### Option 1: On-demand deploy/teardown lane (the 2026-10-18 grooming)

- Pros: $0 standing cost; zero standing attack surface when torn down; destroy-guarded.
- Cons: deploy latency before every remote use; teardown ceremony contradicts a "production" posture; the destroy workflow (#318) exists solely to serve this model.

### Option 2: Always-on production with `minInstances=1`

- Pros: no cold start; simplest availability story.
- Cons: standing compute cost (~$60–70/month class in `europe-west6`) for a single-user service; contradicts the portfolio cost discipline.

### Option 3: Always-on production, scale-to-zero + IAP edge gate (chosen)

- Pros: true production posture with ~$0 idle cost; IAP rejects everyone except the bound account *at the platform edge* — the app is never reached by unauthenticated traffic; the in-app fail-closed OIDC whitelist is retained as defense-in-depth; no load balancer; reuses the groomed bootstrap (WIF keyless deploy, Secret Manager shells).
- Cons: cold start on wake from idle (~30–60s for the 3-container pod with startup probes); instance churn destroys the tmpfs volume (scratch contract — including mid-session on instance replacement); one-time IAP setup (OAuth brand + consent screen, `roles/iap.httpsResourceAccessor` binding).

## Decision Outcome

Chosen option: **"Option 3 — always-on production, scale-to-zero, IAP edge gate"**, because it is the only option that simultaneously satisfies the PO's production directive, the platform-level single-account requirement, and the cost discipline; IAP-direct (GA 2025-04) removes the load-balancer cost that previously made IAP a YAGNI rejection.

Recorded decisions:

- **D1 — Operating model:** Cloud Run `coach-web-509209` is the **always-on production deployment**; `minInstances=0, maxInstances=1` (scale-to-zero). The on-demand deploy/teardown contract of the 2026-10-18 grooming is superseded; the destroy workflow (#318) was removed from scope (closed not-planned) — a bare `gcloud run services delete` runbook line in `docs/admin_guide.md` serves as the emergency off-switch.
- **D2 — Edge posture:** IAP enabled **directly on the Cloud Run service**; `roles/iap.httpsResourceAccessor` bound to `frederic.pitteloud@gmail.com` only (plus a service-scoped grant for the WIF deploy SA used by post-deploy smoke checks). The in-app fail-closed OIDC whitelist (`AUTH_WHITELIST_EMAILS='["frederic.pitteloud@gmail.com"]'`, `AUTH_ENABLED=true`) is **retained as defense-in-depth** — the app-level gate is never the sole control.
- **D3 — Persistence:** tmpfs in-memory volume at `/data`; GCS FUSE rejected (SQLite WAL locking over FUSE is a corruption anti-pattern). The cloud DB is **per-instance scratch** — with scale-to-zero, instance churn is *more* frequent (idle ⇒ instance reclaimed ⇒ `/data` lost, including mid-session). Durable SSOTs remain Intervals.icu + GitHub (#190).
- **D4 — Proxy trust:** `FORWARDED_ALLOW_IPS=*` (Cloud Run override; GFE/IAP-only ingress rationale), `TRUSTED_HOSTS` = the `.run.app` hostname, `X-Forwarded-Proto: https` honored; Dockerfile default `127.0.0.1,::1` keeps local behavior unchanged (#112).
- **D5 — Tooling:** gcloud-based deploy workflows (`.github/workflows/cloud-run-deploy.yaml`, WIF keyless — no service-account JSON keys anywhere); no OpenTofu resurrection (#110/#111 rationale stands).
- **D6 — Lifecycle boundary:** the WIF pool/provider, deploy SA, Secret Manager shells (6 secrets, per-secret `roles/secretmanager.secretAccessor`), and OAuth brand/consent screen are **one-time bootstrap** (#315) that survives service deletion; the Cloud Run service itself is ephemeral and re-deployable from the workflow without secret re-entry.
- **D7 — Coexistence:** the local MADR-0024 VIDAR prod lane is **unchanged and out of scope**; whether it remains, demotes, or retires once Cloud Run production is proven is a separate PO decision.

## Consequences

- **Positive:** the cockpit is reachable from anywhere at a production URL with managed HTTPS; unauthenticated traffic is rejected at the platform edge before reaching the app; ~$0 idle cost; the image supply chain stays release-gated (GHCR prod lane from `v*` tags); re-deploy is one workflow run with zero secret re-entry.
- **Negative:** wake-from-idle cold start (~30–60s) on the first request after inactivity; scratch-data loss on instance churn (mid-session loss possible — an accepted contract); a new GCP surface (IAP config, OAuth consent screen, bindings) must be bootstrapped, audited, and documented; deploy depends on GitHub Actions + WIF availability.
- **Mitigations:** the ephemerality contract is documented plainly in `docs/admin_guide.md` (and the user guide) — cloud sessions are scratch, durable data lives in Intervals.icu/GitHub; `maxInstances=1` bounds cost and abuse; the two-layer access matrix is verified live before closeout (#317, cyber sign-off); WIF keyless auth + per-secret IAM scoping bound the credential blast radius; all resources pinned `europe-west6` (nLPD).

## References

- Epic #313 (Sprint 32 marker) + the 2026-11-14 PO amendment; sub-issues #314 (this record), #315 (bootstrap), #316 (deploy), #317 (zero-trust verification); #318 (destroy — closed not-planned under D1).
- ADR-007 (local Docker baseline); repo-local MADR-0024 (VIDAR prod lane — D7 coexistence); MADR-0008 (encryption at rest carries over — `DATA_ENCRYPTION_KEY` still required at boot); #110/#111 (cloud retirement precedent), #64–#67 (prior cloud topology + WIF/OIDC precedent), #190 (durable SSOTs / ICU mirroring), #112 (forwarded-allow-ips precedent).
- Portfolio: MADR-0017 (trunk+tag), MADR-0008 (security impact routing — cyber review of #316/#317).
- IAP direct-on-Cloud Run availability: IAP release notes, April 2025 (preview announcement; subsequently GA) — no load balancer required.
