# Auditable outputs (AC5): identities, endpoints and federation principals
# surfaced for review. None of these are secrets.

output "cloud_run_service_url" {
  description = "Public URL of the coach-web Cloud Run service."
  value       = module.cloud_run.service_url
}

output "cloud_run_service_name" {
  description = "Name of the Cloud Run service."
  value       = module.cloud_run.service_name
}

output "runtime_sa_email" {
  description = "Runtime service account email (least-privilege telemetry roles bound; secret access is per-secret)."
  value       = module.cloud_run.runtime_sa_email
}

output "cloud_run_secret_ids" {
  description = "Map of purpose -> Secret Manager secret name (issue #66). Values are populated out-of-band; names only — never secret material."
  value       = module.cloud_run.secret_ids
}

output "deployer_sa_email" {
  description = "Keyless CI deployer service account email (impersonated via WIF by GitHub Actions — issue #67)."
  value       = module.wif.deployer_sa_email
}

output "wif_pool_name" {
  description = "Full resource name of the Workload Identity Pool."
  value       = module.wif.pool_name
}

output "wif_provider_name" {
  description = "Full resource name of the GitHub OIDC provider (input for google-github-actions/auth in issue #67)."
  value       = module.wif.provider_name
}

output "wif_principal_set" {
  description = "Repo-scoped principalSet allowed to impersonate the deployer SA."
  value       = module.wif.principal_set_member
}

output "networking_vpc_self_link" {
  description = "Self-link of the VPC foundation network."
  value       = module.networking.vpc_self_link
}

output "networking_subnet_self_link" {
  description = "Self-link of the europe-west6 subnet."
  value       = module.networking.subnet_self_link
}

output "oidc_configured" {
  description = "Whether a Google OIDC client ID has been configured (config surface for issue #65)."
  value       = module.oidc.configured
}
