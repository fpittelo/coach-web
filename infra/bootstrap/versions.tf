# OpenTofu core & provider constraints — coach-web state bucket bootstrap.
# Kept identical to the root module (../versions.tf).

terraform {
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.30"
    }
  }
}
