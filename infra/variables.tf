# ---------------------------------------------------------------------------
# Project & region
# ---------------------------------------------------------------------------

variable "project_id" {
  description = "GCP project ID hosting the coach-web stack. Required — real project IDs are never hardcoded or committed."
  type        = string
}

variable "region" {
  description = "GCP region for every regional resource. europe-west6 (Zürich) keeps personal data under Swiss jurisdiction (nLPD)."
  type        = string
  default     = "europe-west6"

  validation {
    condition     = var.region == "europe-west6"
    error_message = "region is pinned to europe-west6 (Zürich) for Swiss data residency (nLPD, issue #64 AC2). Change it only by consciously editing this validation."
  }
}

# ---------------------------------------------------------------------------
# Cloud Run service skeleton (full multi-container spec lands with issue #66)
# ---------------------------------------------------------------------------

variable "service_name" {
  description = "Cloud Run service name."
  type        = string
  default     = "coach-web"
}

variable "container_image" {
  description = "Placeholder main-container image. The full 3-container topology (coach-web + coach-mcp + github-mcp sidecars) is issue #66."
  type        = string
  default     = "ghcr.io/fpittelo/coach-web:dev"
}

variable "container_cpu" {
  description = "CPU limit for the placeholder container."
  type        = string
  default     = "1"
}

variable "container_memory" {
  description = "Memory limit for the placeholder container."
  type        = string
  default     = "512Mi"
}

variable "min_instance_count" {
  description = "Minimum instances. 0 = scale-to-zero (ADR-04: $0 fixed cost when idle)."
  type        = number
  default     = 0
}

variable "max_instance_count" {
  description = "Maximum instances (cost ceiling for a personal workload)."
  type        = number
  default     = 2
}

variable "allow_unauthenticated" {
  description = "Allow unauthenticated edge invocations (roles/run.invoker to allUsers on this single service). Required by ADR-04: Google OIDC is enforced at application level (whitelist, issue #65), so the edge must pass traffic through to the app."
  type        = bool
  default     = true
}

variable "runtime_sa_id" {
  description = "Service account ID for the Cloud Run runtime identity."
  type        = string
  default     = "coach-web-runtime"
}

# ---------------------------------------------------------------------------
# Networking foundation (direct VPC egress reserved for issue #66)
# ---------------------------------------------------------------------------

variable "vpc_name" {
  description = "Name of the custom-mode VPC foundation network."
  type        = string
  default     = "coach-web-vpc"
}

variable "subnet_name" {
  description = "Name of the europe-west6 subnet."
  type        = string
  default     = "coach-web-subnet"
}

variable "subnet_cidr" {
  description = "CIDR range of the europe-west6 subnet."
  type        = string
  default     = "10.60.0.0/24"
}

variable "enable_vpc_egress" {
  description = "Attach direct VPC egress to the Cloud Run service. Default false — the skeleton needs no private routes; issue #66 may enable it."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Google Identity OIDC — config surface (whitelist enforcement is issue #65)
# ---------------------------------------------------------------------------

variable "oidc_client_id" {
  description = "Google OAuth web client ID (audience the app validates). Empty = not yet configured; the client is created once in the Google Cloud Console."
  type        = string
  default     = ""

  validation {
    condition     = var.oidc_client_id == "" || can(regex("\\.apps\\.googleusercontent\\.com$", var.oidc_client_id))
    error_message = "oidc_client_id must be empty (not yet configured) or a Google OAuth client ID ending in .apps.googleusercontent.com."
  }
}

# ---------------------------------------------------------------------------
# Workload Identity Federation — keyless CI (ADR-06, consumed by issue #67)
# ---------------------------------------------------------------------------

variable "github_org" {
  description = "GitHub organization/user owning the repository allowed to deploy."
  type        = string
  default     = "fpittelo"
}

variable "github_repo" {
  description = "GitHub repository allowed to deploy (per-repo WIF attribute condition, least privilege)."
  type        = string
  default     = "coach-web"
}

variable "wif_pool_id" {
  description = "Workload Identity Pool ID for GitHub Actions federation."
  type        = string
  default     = "github-actions-pool"
}

variable "wif_provider_id" {
  description = "Workload Identity Pool Provider ID (GitHub OIDC)."
  type        = string
  default     = "github-actions-provider"
}

variable "deployer_sa_id" {
  description = "Service account ID for the keyless CI deployer."
  type        = string
  default     = "gha-coach-web-deployer"
}
