# coach-web — OpenTofu root module (issue #64).
#
# Swiss regional GCP foundation (europe-west6, Zürich) for the Phase 2 Cloud
# Run deployment: service skeleton, networking foundation, Google OIDC config
# surface, keyless CI federation (WIF) and remote state (see backend.tf).
#
# ADRs: 03 (sidecar topology), 04 (Google OIDC + app whitelist),
#       05 (Cloud Run + Startup CPU Boost), 06 (keyless WIF deploys).
# Scope boundaries: full multi-container spec -> #66; CI WIF auth -> #67;
#                   app-level whitelist -> #65.

# --- Required Google APIs ----------------------------------------------------
# Each API is enabled explicitly (no implicit enablement at apply time);
# disable_on_destroy = false so destroying this stack never breaks other
# consumers of the project.

resource "google_project_service" "required" {
  for_each = toset([
    "run.googleapis.com",                  # Cloud Run (ADR-05)
    "iam.googleapis.com",                  # service accounts
    "iamcredentials.googleapis.com",       # SA token exchange (WIF impersonation)
    "sts.googleapis.com",                  # Security Token Service (WIF, ADR-06)
    "secretmanager.googleapis.com",        # app secrets (consumed by #66)
    "compute.googleapis.com",              # VPC networking foundation
    "cloudresourcemanager.googleapis.com", # project/IAM metadata
    "serviceusage.googleapis.com",         # API enablement itself
  ])

  service            = each.value
  disable_on_destroy = false
}

# --- Modules -------------------------------------------------------------------

module "networking" {
  source      = "./modules/networking"
  project_id  = var.project_id
  region      = var.region
  vpc_name    = var.vpc_name
  subnet_name = var.subnet_name
  subnet_cidr = var.subnet_cidr

  depends_on = [google_project_service.required]
}

module "oidc" {
  source         = "./modules/oidc"
  oidc_client_id = var.oidc_client_id
}

module "cloud_run" {
  source                = "./modules/cloud-run"
  project_id            = var.project_id
  region                = var.region
  service_name          = var.service_name
  container_image       = var.container_image
  container_cpu         = var.container_cpu
  container_memory      = var.container_memory
  min_instance_count    = var.min_instance_count
  max_instance_count    = var.max_instance_count
  allow_unauthenticated = var.allow_unauthenticated
  runtime_sa_id         = var.runtime_sa_id
  oidc_client_id        = module.oidc.client_id
  oidc_issuer_uri       = module.oidc.issuer_uri
  enable_vpc_egress     = var.enable_vpc_egress
  subnet_self_link      = module.networking.subnet_self_link

  depends_on = [google_project_service.required]
}

module "wif" {
  source          = "./modules/wif"
  project_id      = var.project_id
  github_org      = var.github_org
  github_repo     = var.github_repo
  wif_pool_id     = var.wif_pool_id
  wif_provider_id = var.wif_provider_id
  deployer_sa_id  = var.deployer_sa_id

  depends_on = [google_project_service.required]
}
