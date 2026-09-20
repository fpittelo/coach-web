# Cloud Run multi-container service — coach-web + MCP sidecars (ADR-03, ADR-05).
#
# Scope (issue #66): full 3-container topology replacing the #64 skeleton:
#   coach-web   — main container (FastAPI), the ONLY ingress-terminating
#                 container, service port 8080
#   coach-mcp   — sidecar (SSE, Intervals.icu gateway) on internal port 8000
#   github-mcp  — sidecar (streamable HTTP) on internal port 8001
#
# Cloud Run sidecar pattern: all containers of an instance share the same
# network namespace and communicate over localhost. The Docker DNS names of
# the local compose topology (docker-compose.yml, issue #63) therefore become
# localhost URLs (COACH_MCP_URL / GITHUB_MCP_URL below).
#
# Secrets: three Secret Manager secrets (OpenRouter, Intervals.icu, GitHub
# PAT) with user-managed replication pinned to europe-west6 and PER-SECRET
# IAM bindings for the runtime SA (carried review item #1 from #64). Secret
# VERSIONS are populated out-of-band (see infra/README.md) — no secret
# material is ever managed by, or stored in, IaC or state (AC4).

# --- Runtime identity ---------------------------------------------------------
# Dedicated least-privilege runtime service account. The service never runs on
# the default Compute Engine SA (which carries broad Editor inheritance).

resource "google_service_account" "runtime" {
  project      = var.project_id
  account_id   = var.runtime_sa_id
  display_name = "coach-web Cloud Run runtime"
  description  = "Runtime identity for the coach-web Cloud Run service (issues #64, #66)."
}

# Least-privilege project roles for the runtime SA — each justified:
#   roles/logging.logWriter       -> write application logs to Cloud Logging
#   roles/monitoring.metricWriter -> report container/system metrics
#   roles/cloudtrace.agent        -> export distributed traces
# NOTE: roles/secretmanager.secretAccessor is deliberately NOT granted at
# project level (carried review item #1 from #64). Secret access is bound
# per-secret below, so the runtime SA can read exactly the three secrets this
# stack defines — and nothing else in the project.
resource "google_project_iam_member" "runtime" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/cloudtrace.agent",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

# --- Secret Manager (AC4) -------------------------------------------------------
# Secret metadata + replication only. VERSIONS are populated out-of-band with
# `gcloud secrets versions add` (procedure in infra/README.md); the first
# service deployment requires each secret to already carry a version.

locals {
  # Cloud Run ingress port (issue #66 spec: 8080). The app reads APP_PORT
  # (src/coach_web/config.py) and the Dockerfile ENTRYPOINT resolves it at
  # runtime (default 8000 for the local compose topology). Declaring the
  # service port and the APP_PORT env from this single local keeps them
  # consistent by construction.
  coach_web_port = 8080

  # Sidecar listen ports — mirror docker-compose.yml (issue #63). Sidecar
  # containers share the instance's localhost; these ports are internal-only
  # (AC5) and never exposed as service ports.
  coach_mcp_port  = 8000
  github_mcp_port = 8001

  # Secret definitions: purpose -> { secret_id, description }.
  # Values are populated out-of-band (see infra/README.md) — IaC manages the
  # secret metadata and IAM only, so no secret material ever reaches state.
  app_secrets = {
    openrouter_api_key = {
      secret_id   = var.openrouter_api_key_secret_id
      description = "OpenRouter API key for the coach agent LLM (coach-web container)."
    }
    intervals_api_key = {
      secret_id   = var.intervals_api_key_secret_id
      description = "Intervals.icu API key for the coach-mcp sidecar."
    }
    github_token = {
      secret_id   = var.github_token_secret_id
      description = "GitHub PAT for coach-web (training plan issues) and the github-mcp sidecar."
    }
  }
}

resource "google_secret_manager_secret" "app_secrets" {
  for_each = local.app_secrets

  project   = var.project_id
  secret_id = each.value.secret_id
  # NOTE: `description` is not supported on google_secret_manager_secret in
  # provider 5.45.2; the purpose of each secret is documented in the
  # local.app_secrets map above and in infra/README.md.

  labels = {
    app        = "coach-web"
    managed-by = "tofu"
  }

  # User-managed replication pinned to europe-west6 (Zürich): automatic
  # replication would distribute secret material across Google-managed
  # regions, which is unacceptable for nLPD-scoped credentials (health and
  # training-data access tokens).
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}

# Carried review item #1 (from #64): per-secret IAM instead of the former
# project-level roles/secretmanager.secretAccessor. The runtime SA gains
# read access to EXACTLY these three secrets — least privilege at the
# narrowest scope Secret Manager supports.
resource "google_secret_manager_secret_iam_member" "runtime_accessor" {
  for_each = google_secret_manager_secret.app_secrets

  project   = var.project_id
  secret_id = google_secret_manager_secret.app_secrets[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

# --- Multi-container service (AC1) ----------------------------------------------

resource "google_cloud_run_v2_service" "main" {
  project  = var.project_id
  name     = var.service_name
  location = var.region # europe-west6 (Zürich) — Swiss data residency

  # Carried review item #2 (from #64): INGRESS_TRAFFIC_ALL combined with the
  # allUsers invoker binding below is ONLY acceptable because ADR-04 makes the
  # application-level Google OIDC whitelist (issue #65) the access-control
  # boundary. If #65's posture ever changes, this edge posture must be
  # revisited in the same change.
  ingress = "INGRESS_TRAFFIC_ALL"

  # Carried review item #3 (from #64): deletion_protection is a google
  # provider 6.x feature; the pinned ~> 5.30 line (5.45.2) does not support it
  # on google_cloud_run_v2_service (verified empirically against the provider
  # schema: "Unsupported argument"). Omission documented again; a provider
  # upgrade is a separate cross-module change requiring full re-validation.

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

    # Direct VPC egress (optional): the stack calls public endpoints only
    # (OpenRouter, Intervals.icu, GitHub), so this stays disabled by default;
    # retained from #64 for private connectivity if ever needed.
    dynamic "vpc_access" {
      for_each = var.enable_vpc_egress ? [1] : []
      content {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          subnetwork = var.subnet_self_link
        }
      }
    }

    # --- Container 1 (MAIN): coach-web ----------------------------------------
    # The FIRST container is the ingress-terminating container; its port is
    # the only service port. Sidecar ports below are internal-only (AC5).
    containers {
      name  = "coach-web"
      image = var.coach_web_image

      ports {
        name           = "http1"
        container_port = local.coach_web_port
      }

      resources {
        limits = {
          cpu    = var.coach_web_cpu
          memory = var.coach_web_memory
        }

        # ADR-05: Startup CPU Boost allocates extra CPU during instance
        # start-up to keep cold starts inside the <3.5s budget even from
        # scale-to-zero (AC2 — measurement pending deployment, see README).
        startup_cpu_boost = true
      }

      # Mirrors the compose healthcheck (tests/integration/
      # test_compose_topology.py): GET /health must return 2xx.
      startup_probe {
        http_get {
          path = "/health"
          port = local.coach_web_port
        }
        period_seconds    = 5
        timeout_seconds   = 5
        failure_threshold = 6 # 30s startup budget (compose start_period: 15s)
      }

      # Non-secret configuration as plain env (AC4: secrets only via
      # value_source below — no plaintext credentials anywhere in this spec).
      # KIS (review PR #93): env entries that exactly duplicate the app's
      # built-in defaults (src/coach_web/config.py) are omitted — the app
      # defaults are the single source of truth. Only cloud-required
      # overrides and cloud-specific config are set here.
      env {
        # Cloud override: the app default is 8000 (local compose topology);
        # the Cloud Run ingress port is 8080 (issue #66).
        name  = "APP_PORT"
        value = tostring(local.coach_web_port)
      }
      env {
        # Cloud Run sidecar networking: containers share localhost, replacing
        # the compose DNS names (http://coach-mcp:8000/sse).
        name  = "COACH_MCP_URL"
        value = "http://localhost:${local.coach_mcp_port}/sse"
      }
      env {
        # Streamable HTTP at the root path (compose: http://github-mcp:8001/).
        name  = "GITHUB_MCP_URL"
        value = "http://localhost:${local.github_mcp_port}/"
      }
      dynamic "env" {
        # CORS: the UI is served same-origin by the FastAPI app, so the cloud
        # deployment needs no CORS configuration (empty default). Set
        # cors_origins in tfvars only for a cross-origin consumer; the value
        # must be a JSON array string (Settings.CORS_ORIGINS). Emitted only
        # when non-empty — an empty CORS_ORIGINS env would fail the app's
        # JSON parsing at startup.
        for_each = var.cors_origins != "" ? [1] : []
        content {
          name  = "CORS_ORIGINS"
          value = var.cors_origins
        }
      }

      # OIDC config surface (issue #64): the client ID is injected as env so
      # the app can validate Google ID tokens. Whitelist enforcement is #65.
      env {
        name  = "GOOGLE_OIDC_CLIENT_ID"
        value = var.oidc_client_id
      }
      env {
        name  = "GOOGLE_OIDC_ISSUER"
        value = var.oidc_issuer_uri
      }

      # Secrets via Secret Manager (AC4). version = "latest" resolves at
      # instance start; pin an integer version here for strict deploy
      # reproducibility once rotation cadence is established.
      env {
        name = "OPENROUTER_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.app_secrets["openrouter_api_key"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "GITHUB_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.app_secrets["github_token"].secret_id
            version = "latest"
          }
        }
      }
    }

    # --- Container 2 (SIDECAR): coach-mcp --------------------------------------
    # Intervals.icu MCP gateway, SSE transport — same image and command
    # surface as the compose topology (docker-compose.yml, issue #63).
    containers {
      name  = "coach-mcp"
      image = var.coach_mcp_image

      # Internal-only (AC5): reachable over localhost within the instance.
      # Sidecars must NOT declare a ports block: Cloud Run v2 permits exactly
      # ONE container with an exposed port (the main ingress container) and
      # counts any ports block as an exposed port (API-level validation,
      # invisible to `tofu validate` — live-apply failures #66/#96). The
      # startup probe below references the port number directly.

      resources {
        limits = {
          cpu    = var.coach_mcp_cpu
          memory = var.coach_mcp_memory
        }
      }

      # TCP socket probe: proves the SSE listener is up. HTTP probes MUST NOT
      # target /sse — Server-Sent Events streams never complete, so an HTTP
      # probe always hits its timeout (Cloud Run ERROR_TIMEOUT; each aborted
      # probe also tears the instance down — live incident #66). Compose only
      # appeared to work due to curl header behaviour.
      startup_probe {
        tcp_socket {
          port = local.coach_mcp_port
        }
        period_seconds    = 5
        timeout_seconds   = 5
        failure_threshold = 8 # 40s startup budget (compose start_period: 30s)
      }

      env {
        name  = "MCP_TRANSPORT"
        value = "sse"
      }
      env {
        name  = "MCP_HOST"
        value = "0.0.0.0"
      }
      env {
        name  = "MCP_PORT"
        value = tostring(local.coach_mcp_port)
      }
      env {
        name  = "INTERVALS_ATHLETE_ID"
        value = "0" # Intervals.icu "self" athlete (documented default)
      }
      env {
        name  = "INTERVALS_BASE_URL"
        value = "https://intervals.icu/api/v1"
      }
      env {
        name  = "HTTP_TIMEOUT_SECONDS"
        value = "30.0" # .env.example default
      }
      env {
        name  = "HTTP_MAX_RETRIES"
        value = "3" # .env.example default
      }
      env {
        name  = "CACHE_TTL_SECONDS"
        value = "300" # coach-mcp default (.env.example)
      }
      env {
        name  = "CACHE_TTL_VOLATILE_SECONDS"
        value = "60" # .env.example default
      }

      # Secret via Secret Manager (AC4).
      env {
        name = "INTERVALS_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.app_secrets["intervals_api_key"].secret_id
            version = "latest"
          }
        }
      }
    }

    # --- Container 3 (SIDECAR): github-mcp -------------------------------------
    # Official github-mcp-server in streamable-HTTP mode, same invocation as
    # the compose topology.
    containers {
      name  = "github-mcp"
      image = var.github_mcp_image

      # `args` (NOT `command`): a Cloud Run v2 `command` would REPLACE the
      # image ENTRYPOINT (/server/github-mcp-server) and exec a non-existent
      # `http` binary. Compose `command` maps to CMD (entrypoint args); the
      # Cloud Run equivalent is `args` (review PR #93, blocking).
      args = ["http", "--port", "8001", "--listen-host", "0.0.0.0"]

      # Internal-only (AC5). Sidecars must NOT declare a ports block: Cloud
      # Run v2 permits exactly ONE container with an exposed port (the main
      # ingress container) and counts any ports block as an exposed port
      # (API-level validation, invisible to `tofu validate` — live-apply
      # failures #66/#96). The TCP startup probe below references the port
      # number directly.

      resources {
        limits = {
          cpu    = var.github_mcp_cpu
          memory = var.github_mcp_memory
        }
      }

      # The official image exposes no plain HTTP health endpoint (compose used
      # a binary `--version` check, unavailable as a Cloud Run probe); a TCP
      # socket probe is the closest safe equivalent.
      startup_probe {
        tcp_socket {
          port = local.github_mcp_port
        }
        period_seconds    = 5
        timeout_seconds   = 5
        failure_threshold = 8 # 40s startup budget (compose start_period: 30s)
      }

      env {
        # Comma-separated toolsets for github-mcp-server (documented default).
        name  = "GITHUB_TOOLSETS"
        value = "default"
      }

      # Secret via Secret Manager (AC4): the same GitHub PAT coach-web uses,
      # surfaced under the env name the official image expects.
      env {
        name = "GITHUB_PERSONAL_ACCESS_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.app_secrets["github_token"].secret_id
            version = "latest"
          }
        }
      }
    }
  }
}

# --- Invocation IAM ---------------------------------------------------------------
# Carried review item #2 (from #64): unauthenticated edge + application-level
# Google OIDC whitelist (issue #65) as the access-control boundary. See the
# ingress comment on the service resource above.
# roles/run.invoker on this single service is the narrowest grant that lets
# traffic reach the app; it is not a wildcard role and scopes to nothing
# beyond this service.

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  count    = var.allow_unauthenticated ? 1 : 0
  project  = var.project_id
  name     = google_cloud_run_v2_service.main.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}
