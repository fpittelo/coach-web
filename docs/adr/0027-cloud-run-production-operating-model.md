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
## Amendment 0001 (2026-11-15): deploy/runtime service-account split, WIF narrowed to trunk, all GCP steps approval-gated

**Recorded per the AC5 security verification window on PR #384 (cyber verdict: BLOCK; HIGH finding, CVSS 8.1 — "deploy SA = runtime SA"), architect ruling 2026-11-15, PO informed the same day.**

The original identity model bound the WIF deploy SA `coach-web-deployer@` as the Cloud Run service's **runtime** identity. An application-level compromise therefore inherited `roles/run.admin` (IAM mutation on the project's Cloud Run surface), all six `secretmanager.secretAccessor` payload grants, and the planned `roles/iap.admin` — far beyond what the running application needs. This amendment re-scopes D2/D5/D6 accordingly:

- **D5 (amended) — identity split:** the service's runtime identity is a new, dedicated **`coach-web-runtime@coach-web-509209.iam.gserviceaccount.com`** holding **only** the six per-secret `roles/secretmanager.secretAccessor` bindings — no invoker, no IAP, no IAM-mutation roles (it is the service identity, not an invoker). `coach-web-deployer@` remains the workflow/deploy identity: `roles/run.admin`, `roles/serviceusage.serviceUsageConsumer`, self-`iam.serviceAccountUser`, repo-scoped `iam.workloadIdentityUser`, service-scoped `iap.httpsResourceAccessor` (deploy-SA smoke identity). Blast radius of an app compromise is thereby capped to reading its own runtime secrets.
- **D6 (amended) — least-privilege pre-flight:** the deployer's payload accessor grants on the six secrets are **removed**; pre-flight secret-presence checks use **per-secret `roles/secretmanager.viewer`** (metadata only) instead of a project-level viewer routing.
- **New condition — actAs bridge:** the deployer requires `roles/iam.serviceAccountUser` on the **runtime** SA (the deploy step sets the runtime identity).
- **New condition — WIF narrowed:** the provider `attribute-condition` becomes `assertion.repository=='fpittelo/coach-web' && assertion.ref=='refs/heads/main'` — deploys authenticate only from trunk, post-squash-merge (MADR-0017), never from feature branches or forks.
- **New condition — no ungated GCP auth:** every workflow step that authenticates to GCP — **including the secrets pre-flight** — runs inside the `gcp-cloud-run` GitHub Environment job with `@fpittelo` as required reviewer (the human approval control).

**Execution routing:** the GCP-side deltas (create runtime SA + bindings, actAs bridge, per-secret viewer swap, accessor removals, WIF condition update) are one-time PO-executed Cloud Shell steps, documented in `docs/admin_guide.md` ("One-time bootstrap — Amendment 0001 deltas"); the workflow consumes the end state. Cyber re-verification of the delta precedes any live deploy (AC5 gate on #316).

## Amendment 0002 (2026-11-21): deploy-SA least-privilege — resource-scoped `run.admin`

**Recorded per the @architect ruling on issue #390 (2026-11-21), grounded on official docs (citations inline); implementation @devops (PR "fix(deploy): resource-scoped run.admin — deploy-SA least-privilege (MADR-0027 Am. 0002) (#390)"), verification @cyber-security on #390 (AC4). Verdict per the issue's candidate remediations: (2) REJECTED, (1) ACCEPTED in the resource-scoped form (a), (3) ACCEPTED as a drift guard.**

Finding R1 (cyber scoped ack on PR #389, 2026-11-15): the deploy SA held project-level `roles/run.admin` + actAs-on-runtime, so a deploy-identity compromise could deploy a revision configured with the runtime SA and arbitrary env/secret wiring — full secret-payload read in effect. The Amendment 0001 SA-split capped the **runtime** direction (app compromise inherits only its own accessors) but not the **deploy** direction. This amendment caps it in both directions:

- **D1 — actAs bridge on the runtime SA: KEEP (structurally required) — remediation 2 rejected.** Google's service-identity documentation is explicit: *"the deployer account must be granted the Service Account User role (`roles/iam.serviceAccountUser`) on the service account that is used as the service identity"*, and the role *"contains the `iam.serviceAccounts.actAs` permission, **required to attach a service account** on the service or revision"* (https://docs.cloud.google.com/run/docs/configuring/service-accounts). The deploy flow sets `serviceAccountName` via `gcloud run services replace` (YAML-driven) on **every deploy**, so the actAs bridge cannot be dropped while the dedicated runtime-SA design (Amendment 0001) stands. Verified: no alternative path exists for attaching a user-managed SA.
- **D2 — project-scoped `roles/run.admin` → service-resource-scoped grant (remediation 1, form (a) ACCEPTED).** Grounded: `roles/run.admin` lowest-level grantable resource is the **Cloud Run service** itself (https://docs.cloud.google.com/iam/docs/roles-permissions/run). The deploy surface is exactly one service (`coach-web`, `europe-west6`). The deploy-SA binding moved from project-level to **resource-level** on that service. Effect: the deploy identity can no longer mutate/delete/SSH any *other* Cloud Run resource in the project (jobs, instances, worker pools, future services) and loses the project-wide `run.*` wildcard — the blast radius shrinks from "the project's entire Cloud Run estate" to "the one service it deploys". Sub-option (b) (`run.developer` @ resource + separate conditional `setIamPolicy`) was evaluated and **rejected (YAGNI)**: the workflow legitimately mutates service IAM every deploy (invoker binding for the IAP service agent) and enables IAP, so `run.admin` @ service resource is the minimal-diff re-scope. Residual ACCEPTED and recorded: the deployer retains full control **of the coach-web service itself** — inherent to "may deploy"; the *read* escalation stays capped by the existing invariant stack (per-secret metadata-only deployer per Amendment 0001, no token-minting on the runtime SA = #396 C4). Operationally recorded: `run.services.create` is only grantable at project level, so re-creating a **deleted** service (the emergency off-switch runbook line) requires a one-time PO re-grant or PO-executed recreation — the resource-scoped grant does not cover bootstrap-from-zero.
- **D3 — `roles/iap.admin` project scope: KEEP, explicitly recorded (not silently absorbed).** The workflow mutates IAP policy every deploy (`iap web add-iam-policy-binding` ×2; `iap settings get` drift guard). Project-scoped `iap.admin` is an adjacent residual of the same shape, but this project hosts a single IAP-protected resource today (KIS/YAGNI). Revisit **only** if a second Cloud Run service lands in the project — recorded here so the revisit trigger is explicit.
- **D4 — post-deploy drift guard: ACCEPTED (remediation 3).** A post-deploy read-back assertion in `cloud-run-deploy.yaml`: (i) serving `serviceAccountName` == `coach-web-runtime@…`, (ii) the env `secretKeyRef` surface unchanged (6 secrets, per-container wiring as rendered), fail-closed on mismatch. The ruling's third assertion — `get-iam-policy` invoker set contains no `allUsers`/`allAuthenticatedUsers` — is **subsumed by #396's C1 gate** (which asserts the invoker set == {IAP service agent} exactly, a strictly stronger check) and is deliberately **not duplicated** in the D4 guard.

Escalation chain after this ruling: deploy-identity compromise ⇒ WIF (repo+trunk-conditioned) + env-gated job → resource-scoped `run.admin` (D2) → rogue revision possible **but** bounded: runtime-SA attachment is the design SA, secret read set == the app's own 6 accessors, no token-minting path on the runtime SA (#396 C4), drift guard detects wiring tampering (D4). The Amendment 0001 invariant now holds in **both directions**: app compromise and deploy compromise each inherit only the 6 per-secret accessors.

**Execution routing:** the live migration was executed by @devops on 2026-11-21 in a gcloud-authenticated environment (resource-level `run.admin` added on the coach-web service, project-level binding removed; before/after policy evidence on the PR). The workflow consumes the end state: the D4 drift guard + an IAM-surface gate asserting the deploy SA holds **no project-level** `run.admin` binding (verified via the permission-less `TestIamPermissions` API — `roles/run.admin` does not include `resourcemanager.projects.getIamPolicy`, so the deploy SA can never read the project policy to inspect its own bindings). The one-time grant commands are documented in `docs/admin_guide.md` ("One-time bootstrap — Amendment 0002 deltas"). @cyber-security verification against `arc42/03` §1/§3 precedes closeout (AC4 on #390).

## Amendment 0003 (2026-11-22): standing deploy-lane IAM grants recorded per cyber verification (#420)

**Recorded per the @cyber-security verification of the two read-only grants @devops applied live while restoring the deploy lane (#407 AC3 / #396 C4 gate). Evidence chain: #406 comment 7075977132 (verification request), #406 comment 7075976714 (runtime-SA policy state), #407 comment 7075976280 (deploy-run discharge chain). Verdict 2026-11-22, recorded in #420: Grant 1 **APPROVE**, Grant 2 **APPROVE-WITH-CONDITIONS** — conditions documentation-only, discharged by this amendment plus the matching `docs/admin_guide.md` section. The verdict required no IAM change: both grants are read-only and stand as applied.**

### G1 — `roles/resourcemanager.viewer`, project-level → `coach-web-deployer` (APPROVE)

- **Scope:** project `coach-web-509209` — the only project this lane deploys to.
- **Permission set (official role definition, https://docs.cloud.google.com/iam/docs/roles-permissions/resourcemanager):** `resourcemanager.projects.get`/`list`, `resourcemanager.folders.get`/`list`, `resourcemanager.organizations.get`, `resourcemanager.boundaries.get`/`list`, `resourcemanager.boundaryConfigs.get`, `resourcemanager.capabilities.get`, `resourcemanager.capabilityConfigs.get`/`list`, `resourcemanager.tagKeys.get`/`list`, `resourcemanager.tagValues.get`/`list`, `resourcemanager.tagHolds.list` — 16 permissions, all read-only.
- **Corrected wording (supersedes the earlier shorthand "exactly `resourcemanager.projects.get` + `projects.list`", #420 AC2):** the role is a **read-only project/folder/org viewer plus boundary/capability/tag metadata reads** — broader than the two project permissions the lane consumes, still entirely non-mutating.
- **No policy access, no write surface:** the role contains **no** `resourcemanager.projects.getIamPolicy`/`setIamPolicy` (nor the folder/organization policy permissions), no `*TagBinding*` create/delete, and **no `run.*` permission** of any kind.
- **Purpose:** the `gcloud iap web add-iam-policy-binding --resource-type=cloud-run` step performs a project-level Resource Manager lookup (`resourcemanager.projects.get`) that project-level `run.admin` supplied before Amendment 0002 re-scoped it to the service resource. Without it the lane fails closed at that step (run 44894171347).
- **Amendment 0002 invariant intact:** the deployer still holds **no** project-level `run.admin` (live project-policy read 2026-11-22: `iap.admin` + `resourcemanager.viewer` + `serviceusage.serviceUsageConsumer`), and the workflow's IAM-surface gate is unaffected — the role grants no `run.*` permission.

### G2 — `roles/iam.serviceAccountViewer`, SA-resource-scoped on `coach-web-runtime` → `coach-web-deployer` (APPROVE-WITH-CONDITIONS)

- **Scope:** the single resource `coach-web-runtime@coach-web-509209.iam.gserviceaccount.com` — an SA-resource-level binding (`gcloud iam service-accounts add-iam-policy-binding`), **not** a project-level grant. It confers nothing on any other service account in the project; the role's `resourcemanager.projects.*` entries apply to the bound SA resource only, so the project-level Resource Manager read remains G1's.
- **Exact permission set (official role definition, https://docs.cloud.google.com/iam/docs/roles-permissions/iam):** `iam.serviceAccountKeys.get`, `iam.serviceAccountKeys.list`, `iam.serviceAccounts.get`, `iam.serviceAccounts.getIamPolicy`, `iam.serviceAccounts.list`, `iam.serviceAccounts.listEffectiveTags`, `iam.serviceAccounts.listTagBindings`, `resourcemanager.projects.get`, `resourcemanager.projects.list` — nine permissions, all read-only.
- **Public-key-metadata qualification:** `iam.serviceAccountKeys.get`/`list` return **public key material and key metadata only** (key id, algorithm, key origin/type, validity window, `disabled`). No API returns private key bytes — a user-managed key's private material exists only in the JSON handed to the creator at creation time and is retrievable by no role, this one included. The lane holds no user-managed keys at all (keyless WIF; `keys list --managed-by=user` empty, #315 AC3), so the key surface is empty in practice and, by design, there is **no private-key path**.
- **Explicitly absent (the qualification's other half):** `iam.serviceAccounts.getAccessToken`, `iam.serviceAccounts.getOpenIdToken`, `iam.serviceAccounts.signBlob`, `iam.serviceAccounts.signJwt` (all token/assertion minting), `iam.serviceAccounts.actAs`, and `iam.serviceAccounts.setIamPolicy`. The deployer therefore **cannot mint credentials for the runtime identity, cannot impersonate it, and cannot mutate its policy**. The only actAs path on the runtime SA remains the Amendment 0002 D1 bridge required to attach the service identity — it confers no minting power.
- **Purpose (C4 gate):** the deploy's C4 gate reads the runtime SA's policy (`iam.serviceAccounts.getIamPolicy`) on every run to assert that no principal other than the platform Cloud Run service agent holds a token-minting role on the runtime identity. Without the permission the gate fails closed with PERMISSION_DENIED (run 44897761116): the grant serves the *assertion*, not a mutation surface.
- **Target state holds live:** runtime-SA bindings = `roles/iam.serviceAccountUser` (deployer — Am.0002 D1) + `roles/iam.serviceAccountViewer` (deployer — G2); **no** `iam.serviceAccountTokenCreator`/`iam.serviceAccountOpenIdTokenCreator` (policy etag `BwZgx4TeHzg=`, unchanged since application, #406 comment 7075976714).

### Standing condition — fail-closed trigger

Both grants are read-only and the lane **fails closed without them**: the IAP-binding step and the C4 gate each abort the run with PERMISSION_DENIED (the two incidents above) instead of deploying with a degraded security assertion.

Neither grant is a standing licence for a broader role. **Standing trigger: if Google ever expands either predefined role toward token minting (`getAccessToken`/`getOpenIdToken`/`signBlob`/`signJwt`), `setIamPolicy`, `actAs`, or any write permission, the grant must be replaced — a resource-scoped custom role carrying exactly the permissions enumerated above — or removed before another deploy.** The operational form of this trigger is recorded in `docs/admin_guide.md` → *Standing deploy-lane grants (#420)*; the C4 gate keeps the runtime-SA side asserted live on every deploy.
