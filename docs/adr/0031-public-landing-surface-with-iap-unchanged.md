# MADR-0031: Serve the Public Landing Page from a Separate Static Surface — IAP Posture Unchanged

- **Status:** accepted
- **Date:** 2026-11-22
- **Deciders:** @architect, @fpittelo (PO direction confirmed 2026-11-22: strangers see the landing page without any Google interaction), @cyber-security (verdict APPROVE-WITH-CONDITIONS, comment 7076175268 on #413 — conditions C1–C7 embedded below before acceptance, per AC2 of #413)

## Context

The landing page shipped in #398 renders for any visitor who reaches the application without a
session, and anonymous access at the **application layer** is guaranteed (`/` is in the middleware's
`PUBLIC_PATHS`). In production, however, the console service is gated by **IAP direct-on-service**
(MADR-0027): a truly anonymous visitor — a stranger with no Google session — is intercepted at the
edge and shown the Google sign-in wall, never the landing page. This was verified live in the #317
zero-trust matrix (unauthenticated `GET /` → IAP 302 → Google sign-in, `x-goog-iap-generated-response: true`).

The Product Owner confirmed the intent on 2026-11-22: **strangers on the internet should see the
landing page without any Google interaction.** Today that is impossible by design. Cloud Run's
direct-on-service IAP is all-or-nothing per service — there is **no path-based exemption** — so the
decision is a security-posture change and is routed through the MADR-0008 security-impact path:
`@cyber-security` must verify the chosen option against `arc42/03` §1/§3 **before** the
implementation spec is finalized (AC2 of #413).

## Decision Drivers

- **PO intent:** a public, Google-interaction-free landing page for strangers (marketing/entry surface).
- **Zero-trust edge preservation:** the #317 evidence chain (IAP intercepts all anonymous traffic; the
  in-app OIDC whitelist as defense-in-depth) and the C1/C4 deploy-gate semantics depend on IAP staying
  in front of the console service.
- **Swiss nLPD posture:** the app holds C2-class health data; any widening of its anonymous surface
  must not expose even metadata of that data. The landing content itself is value-free static content
  (no personal data) — nLPD does not constrain it.
- **Single-tenant reality:** one athlete, one admin. Infrastructure growth must be proportionate (KIS/YAGNI).
- **Residency & ops uniformity:** `europe-west6` deployment, gcloud-managed (MADR-0027 §ops), no new
  tooling classes unless justified.

## Considered Options

### Option 1 (chosen): Separate public static landing surface — console IAP unchanged

A second, minimal, **public-by-design** surface serves the landing content (static HTML/CSS reused
from `landing.html`/`landing.css` per KIS). Concretely: a tiny static-serving container deployed to
Cloud Run in `europe-west6` with `--allow-unauthenticated` (invoker `allUsers`), no service account
grants, no environment secrets, no API surface — it serves two static files and a redirect CTA to
the console URL (cross-surface entry → IAP → app). The console service keeps IAP exactly as verified.

- Pros:
  - The zero-trust evidence chain (#317), MADR-0027, and the C1/C4 gate meanings stay **intact** — no
    posture change on the data-holding service.
  - The anonymous surface is **structurally incapable** of leaking app data: no secrets, no API, no
    persistence — the smallest possible blast radius for a public endpoint.
  - Uniform ops (same Cloud Run/deploy-lane pattern, `europe-west6`, gcloud-managed); cost ≈ 0 at
    scale-to-zero for a static container.
  - Content reuse: the existing landing page assets are the source of truth (no fork of the design).
- Cons:
  - A second deployable surface and a second URL to operate and monitor (indirect scope growth).
  - Two-URL experience: the CTA hands the visitor from the public domain to the console URL; the
    canonical-host middleware (#406/#410) already guarantees that handoff is well-defined.
  - The public container's invoker set (`allUsers`) is a standing public endpoint that must be kept
    secret-free by construction and construction-checked (deploy gate analog, kept minimal).

### Option 2 (rejected): Drop IAP on the console service — app layer becomes the only gate

Set `run.invoker` → `allUsers` on `coach-web` and rely solely on the in-app whitelist/session.

- Pros: single surface, no second deployment, the landing is "just" the unauthenticated app render.
- Cons (disqualifying):
  - Exposes the **full application surface — including `/api/*` and the SSE stream — to anonymous
    internet traffic**; the app layer becomes the single gate for a service holding C2 health data.
  - Voids the #317 zero-trust evidence and strips the C1/C4 deploy gates of their meaning
    (`run.services.getIamPolicy` assertions would then pin a public-invoker policy).
  - Blast-radius growth is grossly disproportionate for a single-user app (KIS violation on the
    security side of the ledger).

### Option 3 (rejected): IAP with path exemption via an external load balancer

Front the app with an HTTPS LB + serverless NEG, attach IAP at the LB, and exempt `/` (and static
paths) from IAP at the URL-map level.

- Pros: single surface, path-granular exemptions, IAP retained for everything else.
- Cons (disqualifying): a new infrastructure class for the portfolio (load balancer, NEG, LB-level
  IAP config, new certificate and domain management) — significant cost and operational complexity
  to serve two static files; over-engineering (YAGNI). Cyber precision (verdict C-note): the cost is
  in fact **understated** — an LB-fronted IAP does **not** protect the direct
  `<service>-<hash>.run.app` URL unless that URL is separately ingress-restricted, so Option 3 adds
  a second, weaker edge rather than replacing the problem. Revisit only if the portfolio later needs
  multi-service path-routing anyway.

## Decision Outcome

Chosen option: **"Option 1 — separate public static landing surface, console IAP unchanged"**, because
it satisfies the PO intent (strangers reach the landing with zero Google interaction) while keeping
the verified zero-trust posture of the data-holding service bit-for-bit intact, at near-zero cost and
with the smallest possible anonymous surface (static, secret-free, data-free by construction).
Option 2 trades the entire security edge for convenience; Option 3 buys path granularity the
portfolio does not need with infrastructure it does not want.

## Consequences

- **Positive:** public landing achieved without touching MADR-0027's verified posture; the #317
  evidence chain and C1/C4 gates remain meaningful; the anonymous surface is minimizable and
  auditable (two static files, no config).
- **Negative:** a second surface to deploy, monitor, and keep secret-free; two-URL handoff for the
  visitor; a standing `allUsers` invoker binding on the static service that must remain justification-bound.
- **Mitigations (amended per the @cyber-security verdict, conditions C1–C7 — binding):**
  - **C1/C2 (material — replaces the original "default compute SA" formulation):** the static
    service runs under a **dedicated, purpose-created service account with zero roles** — never the
    default compute SA. Deploy-time assertion must be **exact, project-wide and fail-closed** (the
    #396 C4 pattern): no project- or SA-level role bindings on the landing SA, **no token-minting
    grants** (`iam.serviceAccount*` family excluded, platform service agents excepted). The deploy
    SA holds only `resourcemanager.viewer` (no `getIamPolicy`); where the assertion needs a policy
    *read*, the minimal grant to add is the 1-permission custom role `projects/coach-web-509209/roles/coachWebProjectIamViewer` (`resourcemanager.projects.getIamPolicy`; GCP has no predefined `roles/resourcemanager.projectIamViewer` — the custom role is the @architect-ratified exact-minimal replacement, #417 comment `7078148984`).
  - **C3 (per-service invoker gates):** console invoker stays exactly {IAP service agent}; landing
    invoker == exactly {`allUsers`}; the landing service carries **no** `iap-enabled` annotation.
  - **C4 (MADR-0027 Am.0002 D3 revisit trigger FIRES):** the second Cloud Run service now exists →
    record the `roles/iap.admin` ruling for the landing deploy (grant **resource-scoped to the
    landing service**, mirroring the console's Amendment-0002 scoping).
  - **C5 (public-surface hardening):** non-root container user; no env config, no
    `GOOGLE_APPLICATION_CREDENTIALS`; nothing in the console's path space served; explicit
    `Cache-Control`; `maxInstances`, request timeout, and a budget cap set.
  - **C6 (content provenance):** the static surface's image is **digest-pinned** (trunk-digest deploys
    like the console — see #411's drift guard); content tracked to source.
  - **C7 (CTA safety + documentation):** the CTA links/pins to the **canonical console origin** with a
    constant redirect target (no open-redirect surface; host-only cookies confirmed by the #406/#410
    middleware); official GCP doc citations embedded in the implementation docs;
    `docs/security.md` + the admin guide gain the public-surface section; the MADR trigger extends:
    **any growth of the static surface beyond static files (API, auth, persistence, dynamic
    render) requires a new MADR.**
  - **Sequencing note (verdict cross-link):** #412's `no-store` fix must land on the console **before**
    the public CTA is published — the heuristic-cache incident (#406/#412 forensics) showed the
    console `/` can serve a stale cached landing; publishing a cross-surface CTA before that fix
    would expose that stale-cache window to strangers.
