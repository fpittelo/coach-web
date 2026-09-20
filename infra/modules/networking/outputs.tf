output "vpc_self_link" {
  description = "Self-link of the VPC foundation network."
  value       = google_compute_network.vpc.id
}

output "subnet_self_link" {
  description = "Self-link of the europe-west6 subnet."
  value       = google_compute_subnetwork.subnet.id
}
