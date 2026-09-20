# coach-web — OpenTofu GCP Foundation (europe-west6)

Infrastructure-as-Code for the Phase 2 Serverless Cloud Migration (Sprint 08,
issue #64). Provisions the Swiss regional foundation that issues #65–#67 build
upon: a Cloud Run service skeleton, the networking foundation, the Google
Identity OIDC configuration surface, keyless CI federation (Workload Identity
Federation) and versioned, locked remote state.

## Data residency (Swiss nLPD)

- Every regional resource is pinned to **`europe-west6` (Zürich)**. The region
  variable is guarded by a validation rule — a different region requires a
  conscious edit of `variables.tf`, never a silent override.
- The Workload Identity **pool and provider are global Google resources by
  design**; they hold no personal data (issue #64, AC2). All data-bearing
  resources (Cloud Run service, VPC/subnet, state bucket) live in
  `europe-west6`.
- Biometric/health data (Intervals.icu) never leaves `europe-west6`.

## Module layout

```
infra/
├── main.tf                  # Root: API enablement + module wiring
├── variables.tf             # Strongly typed inputs (region pinned to europe-west6)
├── outputs.tf               # Auditable outputs (URLs, identities, WIF principals)
├── versions.tf              # OpenTofu >= 1.6, google provider ~> 5.30
├── providers.tf             # google provider (ADC locally / WIF in CI from #67)
├── backend.tf               # GCS remote state (europe-west6, versioned + locked)
├── terraform.tfvars.example # Placeholder values — never commit terraform.tfvars
├── bootstrap/               # One-shot config that creates the state bucket
│   ├── main.tf              #   GCS bucket: versioning, UBLA, PAP enforced
│   ├── variables.tf
│   ├── outputs.tf
│   ├── providers.tf
│   ├── versions.tf
│   └── terraform.tfvars.example
└── modules/
    ├── cloud-run/           # Service skeleton: placeholder container, runtime SA,
    │                        # Startup CPU Boost, scale-to-zero, invocation IAM
    ├── networking/          # Custom-mode VPC + europe-west6 subnet (Private
    │                        # Google Access) — direct VPC egress foundation (#66)
    ├── oidc/                # Google Identity OIDC config surface (client ID,
    │                        # issuer) — whitelist enforcement is #65
    └── wif/                 # WIF pool + GitHub OIDC provider (repo-scoped
                             # attribute condition) + least-privilege deployer SA
```

## Remote state & bootstrap (AC3)

State lives in a GCS bucket in `europe-west6` with object **versioning**
(state history) and the GCS backend's native **state locking**. The bucket
cannot create itself, so a tiny separate configuration with a **local backend**
bootstraps it once:

```bash
# 1. Authenticate as yourself (no service account keys — AC6)
gcloud auth application-default login

# 2. Create the state bucket (one-time)
cd infra/bootstrap
cp terraform.tfvars.example terraform.tfvars   # set project_id
tofu init
tofu plan          # review: one regional bucket
tofu apply         # procedural gate: see "Apply policy" below

# 3. Switch the root module to remote state
cd ..
tofu init          # configures the GCS backend from backend.tf
```

The bucket name in `backend.tf` must match `state_bucket_name` in
`infra/bootstrap/variables.tf` (backend blocks cannot use variables). Defaults
are kept in sync: `fpittelo-coach-web-tofu-state-europe-west6`.

## Running `tofu plan` locally

```bash
cd infra
gcloud auth application-default login              # if not already authenticated
tofu init                                          # remote backend + providers
cp terraform.tfvars.example terraform.tfvars       # set project_id (once)
tofu plan -out=tfplan                              # auditable diff (AC5)
tofu show tfplan                                   # human-readable review
```

`tofu plan` performs no writes and is safe to run at any time. The resulting
plan diff is attached to the corresponding issue/PR as the audit record.

## Apply policy (AC5 — procedural gate)

**`tofu apply` is never wired into CI.** Applies are executed manually by
@devops **only after an explicit approval comment from @fpittelo** on the
corresponding issue or promotion PR. This gate is procedural by design: the
audit trail lives in GitHub (approval comment → apply → plan diff attached to
the issue), not in pipeline YAML.

## Workload Identity Federation (AC6 — keyless, ADR-06)

- Pool `github-actions-pool` + provider `github-actions-provider` trust
  `https://token.actions.githubusercontent.com`.
- The **attribute condition** restricts federation to
  `assertion.repository == "fpittelo/coach-web"` — tokens from any other
  repository are rejected outright.
- The deployer service account is impersonated via a **repo-scoped
  `principalSet`** bound on the SA itself (not project-wide).
- **No service account keys exist anywhere** — not in code, not in state.
  GitHub Actions consumes this in issue #67 (`deploy.yaml`), using the
  outputs `wif_provider_name` and `deployer_sa_email`.

## IAM — least-privilege justifications

| Principal | Role | Why |
| --- | --- | --- |
| `gha-coach-web-deployer` (CI) | `roles/run.admin` | Create/update the Cloud Run service during deploys (#67) |
| `gha-coach-web-deployer` (CI) | `roles/iam.serviceAccountUser` | Set the runtime SA on the service it deploys |
| `coach-web-runtime` | `roles/logging.logWriter` | Write application logs |
| `coach-web-runtime` | `roles/monitoring.metricWriter` | Report container metrics |
| `coach-web-runtime` | `roles/cloudtrace.agent` | Export traces |
| `coach-web-runtime` | `roles/secretmanager.secretAccessor` | Read app secrets at runtime (#66) |
| `allUsers` | `roles/run.invoker` (single service) | ADR-04: edge accepts traffic, app enforces the Google OIDC whitelist (#65) |

Deliberately **not** granted: `roles/owner`, any `roles/*` wildcard,
`secretmanager.secretAccessor` on the CI deployer (it never reads secret
values), Artifact Registry roles (images ship from public ghcr.io).

## CI gates (AC4, AC7)

`.github/workflows/ci.yaml` runs, with zero-warning tolerance:

1. `tofu fmt -check -recursive infra/`
2. `tofu init -backend=false` + `tofu validate` in `infra/`
3. `tofu init -backend=false` + `tofu validate` in `infra/bootstrap/`

`-backend=false` because CI holds no GCP credentials in this issue; WIF-based
CI authentication (and any plan/apply automation) is issue #67.

## Intentionally out of scope (tracked elsewhere)

| Concern | Issue |
| --- | --- |
| Full multi-container Cloud Run spec (coach-web + 2 MCP sidecars) | #66 |
| Application-level Google OIDC whitelist | #65 |
| GitHub Actions WIF authentication in `deploy.yaml` | #67 |
