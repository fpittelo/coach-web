# Cloud Run service skeleton — coach-web (ADR-05).
#
# Scope (issue #64): infrastructure foundation only — service skeleton with a
# single placeholder container, region pinning, runtime identity, Startup CPU
# Boost and invocation IAM. The full multi-container spec (coach-web +
# coach-mcp + github-mcp sidecars, ports, secrets, health checks) is issue #66.

# --- Runtime identity ---------------------------------------------------------
# Dedicated least-privilege runtime service account. The service never runs on
# the default Compute Engine SA (which carries broad Editor inheritance).

resource "google_service_account" "runtime" {
  project      = var.project_id
  account_id   = var.runtime_sa_id
  display_name = "coach-web Cloud Run runtime"
  description  = "Runtime identity for the coach-web Cloud Run service (issue #64)."
}

# Least-privilege project roles for the runtime SA — each justified:
#   roles/logging.logWriter            -> write application logs to Cloud Logging
#   roles/monitoring.metricWriter      -> report container/system metrics
#   roles/cloudtrace.agent             -> export distributed traces
#   roles/secretmanager.secretAccessor -> read app secrets at runtime (OpenRouter,
#     Intervals.icu and GitHub credentials wired as secrets in issue #66).
#     Granted here, NOT to the CI deployer SA: the deployer never needs to read
#     secret values (least privilege).
resource "google_project_iam_member" "runtime" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/cloudtrace.agent",
    "roles/secretmanager.secretAccessor",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

# --- Service skeleton -----------------------------------------------------------

resource "google_cloud_run_v2_service" "main" {
  project  = var.project_id
  name     = var.service_name
  location = var.region # europe-west6 (Zürich) — Swiss data residency (AC2)

  # ADR-04: Google OIDC is enforced at application level (whitelist, issue #65),
  # so the edge must accept unauthenticated HTTP and hand it to the app.
  ingress = "INGRESS_TRAFFIC_ALL"

  # NOTE: deletion_protection is a google provider 6.x feature; with the pinned
  # ~> 5.30 line it is unavailable. Revisit with the full service spec (#66).

  labels = {
    app        = "coach-web"
    managed-by = "tofu"
  }

  template {
    service_account = google_service_account.runtime.email

    # ADR-04: scale-to-zero keeps the fixed cost at $0 when idle.
    scaling {
      min_instance_count = var.min_instance_count
      max_instance_count = var.max_instance_count
    }

    # Direct VPC egress (optional): reserved for issue #66 if private
    # connectivity is needed; disabled by default to keep the skeleton minimal.
    dynamic "vpc_access" {
      for_each = var.enable_vpc_egress ? [1] : []
      content {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          subnetwork = var.subnet_self_link
        }
      }
    }

    containers {
      # Placeholder single-container image; the full 3-container topology
      # (coach-web + coach-mcp + github-mcp sidecars) lands with issue #66.
      image = var.container_image

      ports {
        name           = "http1"
        container_port = 8080 # cloud port per ADR-03 (local compose uses 8000)
      }

      resources {
        limits = {
          cpu    = var.container_cpu
          memory = var.container_memory
        }

        # ADR-05: Startup CPU Boost allocates CPU during instance start-up to
        # keep cold starts under the 3.5s budget even at scale-to-zero.
        # (Cloud Run v2 API: startupCpuBoost lives on ResourceRequirements.)
        startup_cpu_boost = true
      }

      # OIDC config surface (issue #64): the client ID is injected as env so the
      # app can validate Google ID tokens. Whitelist enforcement is issue #65.
      env {
        name  = "GOOGLE_OIDC_CLIENT_ID"
        value = var.oidc_client_id
      }
      env {
        name  = "GOOGLE_OIDC_ISSUER"
        value = var.oidc_issuer_uri
      }
    }
  }
}

# --- Invocation IAM ---------------------------------------------------------------
# ADR-04: unauthenticated edge + application-level Google OIDC whitelist.
# roles/run.invoker on this single service is the narrowest grant that lets
# traffic reach the app; it is not a wildcard role and scopes to nothing beyond
# this service.

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  count    = var.allow_unauthenticated ? 1 : 0
  project  = var.project_id
  name     = google_cloud_run_v2_service.main.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}
