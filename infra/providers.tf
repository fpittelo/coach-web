# Google Cloud provider — coach-web foundation (europe-west6, Zürich).
#
# Credentials are NEVER hardcoded (AC6 — no service account keys anywhere):
#   - local runs: Application Default Credentials
#     (`gcloud auth application-default login`)
#   - CI runs:    Workload Identity Federation (wired in issue #67; this
#     issue's CI only runs fmt/validate with -backend=false and no auth).

provider "google" {
  project = var.project_id
  region  = var.region
}
