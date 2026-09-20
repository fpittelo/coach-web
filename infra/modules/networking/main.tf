# Networking foundation — coach-web (europe-west6).
#
# Custom-mode VPC + one regional subnet in europe-west6 (Zürich) with Private
# Google Access. Reserved as the direct-VPC-egress foundation for the full
# Cloud Run topology (issue #66); the service skeleton keeps egress disabled
# until something actually needs private routes.

resource "google_compute_network" "vpc" {
  project                 = var.project_id
  name                    = var.vpc_name
  auto_create_subnetworks = false # custom mode — only our europe-west6 subnet exists
  description             = "coach-web VPC foundation (issue #64) — europe-west6 only."
}

resource "google_compute_subnetwork" "subnet" {
  project                  = var.project_id
  name                     = var.subnet_name
  region                   = var.region # europe-west6 (Zürich) — AC2
  network                  = google_compute_network.vpc.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true # private access to Google APIs without external IPs
}
