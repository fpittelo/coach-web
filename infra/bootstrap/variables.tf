variable "project_id" {
  description = "GCP project ID that will host the remote state bucket."
  type        = string
}

variable "region" {
  description = "GCP region for the state bucket. europe-west6 (Zürich) keeps state under Swiss jurisdiction (nLPD, AC3)."
  type        = string
  default     = "europe-west6"

  validation {
    condition     = var.region == "europe-west6"
    error_message = "region is pinned to europe-west6 (Zürich) for Swiss data residency (nLPD, issue #64 AC2/AC3). Change it only by consciously editing this validation."
  }
}

variable "state_bucket_name" {
  description = "Globally unique GCS bucket name for remote state. MUST match the backend block in ../backend.tf."
  type        = string
  default     = "fpittelo-coach-web-tofu-state-europe-west6"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9_-]{1,61}[a-z0-9]$", var.state_bucket_name))
    error_message = "GCS bucket names must be 3-63 chars: lowercase letters, numbers, dashes, underscores."
  }
}
