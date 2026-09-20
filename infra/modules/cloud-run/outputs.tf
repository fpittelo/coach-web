output "service_url" {
  description = "Public URL of the Cloud Run service."
  value       = google_cloud_run_v2_service.main.uri
}

output "service_name" {
  description = "Name of the Cloud Run service."
  value       = google_cloud_run_v2_service.main.name
}

output "runtime_sa_email" {
  description = "Email of the dedicated runtime service account."
  value       = google_service_account.runtime.email
}

output "secret_ids" {
  description = "Map of purpose -> Secret Manager secret name. Values are populated out-of-band (see infra/README.md); names only — never secret material."
  value       = { for purpose, secret in google_secret_manager_secret.app_secrets : purpose => secret.secret_id }
}
