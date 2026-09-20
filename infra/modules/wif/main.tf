# Workload Identity Federation — keyless GitHub Actions deployment (ADR-06).
#
# Issue #64 scope: trust configuration + least-privilege CI deployer identity.
# Wiring GitHub Actions to actually use it (id-token auth in deploy.yaml) is
# issue #67. AC6: no service account keys are created anywhere — federation
# only.

# --- Identity pool & provider (global resources by design) ---------------------
# WIF pools/providers are global Google resources and hold no personal data;
# per issue #64 AC2 this is acceptable. All data-bearing resources are pinned
# to europe-west6.

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = var.wif_pool_id
  display_name              = "GitHub Actions (fpittelo)"
  description               = "Keyless CI federation for fpittelo repositories (ADR-06, issue #64)."
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = var.wif_provider_id
  display_name                       = "GitHub Actions OIDC provider"
  description                        = "GitHub Actions OIDC tokens, restricted to repository ${var.github_org}/${var.github_repo} deploy refs (dev/qa/main branches, v* tags)."

  # Ref-scoped attribute condition (PR #95 review, issue #67): only tokens
  # from ${var.github_org}/${var.github_repo} AND from refs that legitimately
  # deploy may ever authenticate — the dev/qa/main branches (main covers
  # workflow_dispatch, which runs on the default branch) and version tags
  # (CEL startsWith covers the v* family). PR refs (refs/pull/*), feature
  # branches and other tags are rejected at the STS token exchange, before
  # any IAM evaluation.
  attribute_condition = "assertion.repository == \"${var.github_org}/${var.github_repo}\" && (assertion.ref == \"refs/heads/dev\" || assertion.ref == \"refs/heads/qa\" || assertion.ref == \"refs/heads/main\" || assertion.ref.startsWith(\"refs/tags/v\"))"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
    "attribute.sha"        = "assertion.sha"
  }

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
    # GitHub Actions OIDC tokens carry aud = "https://github.com/<owner>".
    #
    # Carried review item #1 (from #64, resolved in #67): google-github-actions/
    # auth defaults its requested audience to the PROVIDER RESOURCE NAME (which
    # embeds the GCP project number — unknowable in this module without a
    # project-number data lookup, and impossible to reference from this
    # resource's own arguments). The deploy workflow therefore sets
    # `audience: https://github.com/<org>` explicitly (deploy.yaml), pairing
    # exactly with allowed_audiences below. The per-repo attribute_condition
    # above remains the actual trust boundary.
    allowed_audiences = ["https://github.com/${var.github_org}"]
  }
}

# --- CI deployer service account -------------------------------------------------

resource "google_service_account" "deployer" {
  project      = var.project_id
  account_id   = var.deployer_sa_id
  display_name = "GitHub Actions deployer (coach-web)"
  description  = "Keyless CI identity used by GitHub Actions via WIF (issue #64; consumed by #67)."
}

# Repo-scoped principalSet: only tokens from fpittelo/coach-web may impersonate
# the deployer SA. Bound on the SA itself (not project-wide) for least privilege.
resource "google_service_account_iam_member" "deployer_wif" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_org}/${var.github_repo}"
}

# Least-privilege project role for the deployer SA:
#   roles/run.admin -> create/update the Cloud Run service (deploys, #67)
#
# Carried review item #2 (from #64, resolved in #67): roles/iam.serviceAccountUser
# was removed from this PROJECT-WIDE grant. `tofu apply` only needs act-as on
# the single Cloud Run runtime SA it sets on the service; that binding is now
# scoped to the runtime SA itself (google_service_account_iam_member in the
# root main.tf).
#
# Deliberately NOT granted (least privilege):
#   roles/secretmanager.secretAccessor -> the deployer never reads secret values;
#     runtime secret access is bound to the runtime SA (modules/cloud-run).
#   roles/artifactregistry.reader      -> images ship from public ghcr.io, no AR pull.
#   project-level storage roles        -> remote-state access is bound to the
#     state bucket ONLY (google_storage_bucket_iam_member in root main.tf, #67).
resource "google_project_iam_member" "deployer" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}
