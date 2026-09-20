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
  description                        = "GitHub Actions OIDC tokens, restricted to repository ${var.github_org}/${var.github_repo}."

  # Per-repo attribute condition (least privilege): only tokens issued to
  # fpittelo/coach-web can ever authenticate. Issue #67 may tighten this
  # further to specific refs (e.g. refs/heads/dev).
  attribute_condition = "assertion.repository == \"${var.github_org}/${var.github_repo}\""

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
    "attribute.sha"        = "assertion.sha"
  }

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
    # GitHub Actions OIDC tokens carry aud = "https://github.com/<owner>".
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

# Least-privilege project roles for the deployer SA — each justified:
#   roles/run.admin              -> create/update the Cloud Run service (deploys, #67)
#   roles/iam.serviceAccountUser -> act as the runtime SA when setting it on the service
#
# Deliberately NOT granted (least privilege):
#   roles/secretmanager.secretAccessor -> the deployer never reads secret values;
#     runtime secret access is bound to the runtime SA (modules/cloud-run).
#   roles/artifactregistry.reader      -> images ship from public ghcr.io, no AR pull.
#   state-bucket access                -> CI does not manage remote state in this
#     issue; if #67 needs it, grant roles/storage.objectAdmin on the bucket only.
resource "google_project_iam_member" "deployer" {
  for_each = toset([
    "roles/run.admin",
    "roles/iam.serviceAccountUser",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deployer.email}"
}
