# Bootstrap runs locally with Application Default Credentials (no SA keys, AC6):
#   gcloud auth application-default login

provider "google" {
  project = var.project_id
  region  = var.region
}
