variable "project_id" {
  description = "GCP project ID hosting the coach-web stack."
  type        = string
}

variable "region" {
  description = "GCP region for the Cloud Run service (europe-west6, Zürich)."
  type        = string
}

variable "service_name" {
  description = "Cloud Run service name."
  type        = string
}

variable "container_image" {
  description = "Placeholder main-container image (full topology is issue #66)."
  type        = string
}

variable "container_cpu" {
  description = "CPU limit for the placeholder container."
  type        = string
}

variable "container_memory" {
  description = "Memory limit for the placeholder container."
  type        = string
}

variable "min_instance_count" {
  description = "Minimum instances (0 = scale-to-zero, ADR-04)."
  type        = number
}

variable "max_instance_count" {
  description = "Maximum instances (cost ceiling)."
  type        = number
}

variable "allow_unauthenticated" {
  description = "Grant roles/run.invoker to allUsers on this service (ADR-04 app-level OIDC)."
  type        = bool
}

variable "runtime_sa_id" {
  description = "Service account ID for the Cloud Run runtime identity."
  type        = string
}

variable "oidc_client_id" {
  description = "Google OAuth web client ID injected as GOOGLE_OIDC_CLIENT_ID (config surface, issue #65)."
  type        = string
}

variable "oidc_issuer_uri" {
  description = "Google OIDC issuer URI injected as GOOGLE_OIDC_ISSUER."
  type        = string
}

variable "enable_vpc_egress" {
  description = "Attach direct VPC egress to the service (reserved for issue #66)."
  type        = bool
}

variable "subnet_self_link" {
  description = "Self-link of the europe-west6 subnet used for direct VPC egress."
  type        = string

  validation {
    condition     = !var.enable_vpc_egress || var.subnet_self_link != ""
    error_message = "enable_vpc_egress = true requires a non-empty subnet_self_link."
  }
}
