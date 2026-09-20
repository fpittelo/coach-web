variable "project_id" {
  description = "GCP project ID hosting the coach-web stack."
  type        = string
}

variable "github_org" {
  description = "GitHub organization/user owning the repository allowed to deploy."
  type        = string
}

variable "github_repo" {
  description = "GitHub repository allowed to deploy (per-repo attribute condition)."
  type        = string
}

variable "wif_pool_id" {
  description = "Workload Identity Pool ID for GitHub Actions federation."
  type        = string
}

variable "wif_provider_id" {
  description = "Workload Identity Pool Provider ID (GitHub OIDC)."
  type        = string
}

variable "deployer_sa_id" {
  description = "Service account ID for the keyless CI deployer."
  type        = string
}
