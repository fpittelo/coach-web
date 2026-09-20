output "state_bucket_name" {
  description = "Name of the remote state bucket. Must match the backend block in ../backend.tf."
  value       = google_storage_bucket.tfstate.name
}

output "state_bucket_url" {
  description = "gs:// URI of the remote state bucket."
  value       = google_storage_bucket.tfstate.url
}
