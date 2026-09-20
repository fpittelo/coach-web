# OpenTofu core & provider version constraints — coach-web GCP foundation.
#
# Issue #64 (Sprint 08, Phase 2: Serverless Cloud Migration).
# ADR-04 (Google OIDC + application-level whitelist),
# ADR-05 (Cloud Run multi-container + Startup CPU Boost),
# ADR-06 (keyless deploys via Workload Identity Federation).

terraform {
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.30"
    }
  }
}
