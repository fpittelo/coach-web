# coach-web — OpenTofu root module (issues #64, #66).
#
# Swiss regional GCP foundation (europe-west6, Zürich) for the Phase 2 Cloud
# Run deployment: multi-container Cloud Run service (coach-web + coach-mcp +
# github-mcp sidecars), networking foundation, Google OIDC config surface,
# keyless CI federation (WIF) and remote state (see backend.tf).
#
# ADRs: 03 (sidecar topology), 04 (Google OIDC + app whitelist),
#       05 (Cloud Run + Startup CPU Boost), 06 (keyless WIF deploys).
# Scope boundaries: app-level whitelist -> #65; CI WIF auth -> #67.

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
    "secretmanager.googleapis.com",        # app secrets (AC4, issue #66)
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
  source       = "./modules/cloud-run"
  project_id   = var.project_id
  region       = var.region
  service_name = var.service_name

  # Multi-container topology (issue #66, AC1/AC3)
  coach_web_image   = var.coach_web_image
  coach_mcp_image   = var.coach_mcp_image
  github_mcp_image  = var.github_mcp_image
  coach_web_cpu     = var.coach_web_cpu
  coach_web_memory  = var.coach_web_memory
  coach_mcp_cpu     = var.coach_mcp_cpu
  coach_mcp_memory  = var.coach_mcp_memory
  github_mcp_cpu    = var.github_mcp_cpu
  github_mcp_memory = var.github_mcp_memory

  # Scaling (ADR-04: scale-to-zero)
  min_instance_count = var.min_instance_count
  max_instance_count = var.max_instance_count

  # Invocation & identity (ADR-04)
  allow_unauthenticated = var.allow_unauthenticated
  runtime_sa_id         = var.runtime_sa_id
  oidc_client_id        = module.oidc.client_id
  oidc_issuer_uri       = module.oidc.issuer_uri

  # Secret Manager secret names (AC4 — versions populated out-of-band)
  openrouter_api_key_secret_id = var.openrouter_api_key_secret_id
  intervals_api_key_secret_id  = var.intervals_api_key_secret_id
  github_token_secret_id       = var.github_token_secret_id

  # Non-secret application configuration (cloud-specific only — review PR #93)
  cors_origins = var.cors_origins

  # Networking (optional direct VPC egress)
  enable_vpc_egress = var.enable_vpc_egress
  subnet_self_link  = module.networking.subnet_self_link

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

# --- CI deployer grants (issue #67) ---------------------------------------------
# Two least-privilege bindings complete the keyless deploy path; both resolve
# carried review items from #64/#66.

# 1. Act-as the Cloud Run runtime SA (carried review item #2 from #64).
# `tofu apply` sets `service_account` on the Cloud Run service, which requires
# roles/iam.serviceAccountUser on THAT service account. The former PROJECT-WIDE
# grant on the deployer (modules/wif) is replaced by this SA-scoped binding.
resource "google_service_account_iam_member" "deployer_runtime_sa_user" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${module.cloud_run.runtime_sa_email}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${module.wif.deployer_sa_email}"
}

# 2. Remote-state access for CI (the deploy workflow runs `tofu init` against
# the GCS backend, which reads/writes state objects and lock files). Scoped to
# the state bucket ONLY — no project-level storage roles. The bucket is created
# by infra/bootstrap (chicken-and-egg: it cannot create itself); this binding
# is created by the first apply, which always runs locally with human ADC (see
# README "Apply policy"), so CI never bootstraps its own state access.
resource "google_storage_bucket_iam_member" "deployer_state_access" {
  bucket = var.state_bucket_name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${module.wif.deployer_sa_email}"
}
