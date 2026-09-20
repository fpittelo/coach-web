# Bootstrap — creates the remote state bucket (chicken-and-egg solver).
#
# This config intentionally uses the DEFAULT LOCAL backend: it is the one piece
# of infrastructure that must exist before remote state can be used. Run it
# once, by hand, then never again (unless the bucket is lost).
#
# The bucket name must match the literal in ../backend.tf (backend blocks
# cannot reference variables).

resource "google_project_service" "storage" {
  project            = var.project_id
  service            = "storage.googleapis.com"
  disable_on_destroy = false
}

resource "google_storage_bucket" "tfstate" {
  project  = var.project_id
  name     = var.state_bucket_name
  location = var.region # europe-west6 (Zürich) — state never leaves Switzerland (AC3)

  # Hardening: uniform ACLs, no public access, state is never silently destroyed.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  # State history: every apply is versioned; versions are pruned once 20 newer
  # versions exist (auditable history without unbounded growth).
  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      num_newer_versions = 20
    }
    action {
      type = "Delete"
    }
  }

  labels = {
    app        = "coach-web"
    managed-by = "tofu"
    purpose    = "tofu-state"
  }

  depends_on = [google_project_service.storage]
}
