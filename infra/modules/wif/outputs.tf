output "pool_name" {
  description = "Full resource name of the Workload Identity Pool."
  value       = google_iam_workload_identity_pool.github.name
}

output "provider_name" {
  description = "Full resource name of the GitHub OIDC provider."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "deployer_sa_email" {
  description = "Email of the keyless CI deployer service account."
  value       = google_service_account.deployer.email
}

output "principal_set_member" {
  description = "Repo-scoped principalSet allowed to impersonate the deployer SA."
  value       = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_org}/${var.github_repo}"
}
