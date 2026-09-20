# ---------------------------------------------------------------------------
# Project & region
# ---------------------------------------------------------------------------

variable "project_id" {
  description = "GCP project ID hosting the coach-web stack. Required — real project IDs are never hardcoded or committed."
  type        = string
}

variable "region" {
  description = "GCP region for every regional resource. europe-west6 (Zürich) keeps personal data under Swiss jurisdiction (nLPD)."
  type        = string
  default     = "europe-west6"

  validation {
    condition     = var.region == "europe-west6"
    error_message = "region is pinned to europe-west6 (Zürich) for Swiss data residency (nLPD, issue #64 AC2). Change it only by consciously editing this validation."
  }
}

# ---------------------------------------------------------------------------
# Cloud Run multi-container service (issue #66, ADR-03/ADR-05)
# ---------------------------------------------------------------------------

variable "service_name" {
  description = "Cloud Run service name."
  type        = string
  default     = "coach-web"
}

variable "coach_web_image" {
  description = "coach-web main-container image (FastAPI). Pinned tag or digest; the deploy workflow (#67) wires environment-specific tags (dev/qa/prod/sha)."
  type        = string
  default     = "ghcr.io/fpittelo/coach-web:dev"

  validation {
    condition     = can(regex(":[^/@]+$", var.coach_web_image)) && !can(regex(":latest$", var.coach_web_image))
    error_message = "coach_web_image must pin an explicit tag or digest (AC3); floating ':latest' is rejected."
  }
}

variable "coach_mcp_image" {
  description = "coach-mcp sidecar image (Intervals.icu MCP gateway, SSE)."
  type        = string
  default     = "ghcr.io/fpittelo/coach:dev"

  validation {
    condition     = can(regex(":[^/@]+$", var.coach_mcp_image)) && !can(regex(":latest$", var.coach_mcp_image))
    error_message = "coach_mcp_image must pin an explicit tag or digest (AC3); floating ':latest' is rejected."
  }
}

variable "github_mcp_image" {
  description = "github-mcp sidecar image (official github-mcp-server, streamable HTTP)."
  type        = string
  default     = "ghcr.io/github/github-mcp-server:v1.12.2"

  validation {
    condition     = can(regex(":[^/@]+$", var.github_mcp_image)) && !can(regex(":latest$", var.github_mcp_image))
    error_message = "github_mcp_image must pin an explicit tag or digest (AC3); floating ':latest' is rejected."
  }
}

# Per-container resources — sized for scale-to-zero ($0 at idle, ADR-04).

variable "coach_web_cpu" {
  description = "CPU limit for the coach-web main container."
  type        = string
  default     = "1"
}

variable "coach_web_memory" {
  description = "Memory limit for the coach-web main container."
  type        = string
  default     = "512Mi"
}

variable "coach_mcp_cpu" {
  description = "CPU limit for the coach-mcp sidecar."
  type        = string
  default     = "0.5"
}

variable "coach_mcp_memory" {
  description = "Memory limit for the coach-mcp sidecar."
  type        = string
  default     = "256Mi"
}

variable "github_mcp_cpu" {
  description = "CPU limit for the github-mcp sidecar."
  type        = string
  default     = "0.25"
}

variable "github_mcp_memory" {
  description = "Memory limit for the github-mcp sidecar."
  type        = string
  default     = "256Mi"
}

variable "min_instance_count" {
  description = "Minimum instances. 0 = scale-to-zero (ADR-04: $0 fixed cost when idle)."
  type        = number
  default     = 0
}

variable "max_instance_count" {
  description = "Maximum instances (cost ceiling for a personal workload)."
  type        = number
  default     = 2
}

variable "allow_unauthenticated" {
  description = "Allow unauthenticated edge invocations (roles/run.invoker to allUsers on this single service). Required by ADR-04: Google OIDC is enforced at application level (whitelist, issue #65), so the edge must pass traffic through to the app."
  type        = bool
  default     = true
}

variable "runtime_sa_id" {
  description = "Service account ID for the Cloud Run runtime identity."
  type        = string
  default     = "coach-web-runtime"
}

# ---------------------------------------------------------------------------
# Secret Manager secret names (AC4 — versions populated out-of-band)
# ---------------------------------------------------------------------------

variable "openrouter_api_key_secret_id" {
  description = "Secret Manager secret ID holding the OpenRouter API key (coach-web)."
  type        = string
  default     = "openrouter-api-key"
}

variable "intervals_api_key_secret_id" {
  description = "Secret Manager secret ID holding the Intervals.icu API key (coach-mcp sidecar)."
  type        = string
  default     = "intervals-api-key"
}

variable "github_token_secret_id" {
  description = "Secret Manager secret ID holding the GitHub PAT (coach-web + github-mcp sidecar)."
  type        = string
  default     = "github-token"
}

variable "auth_session_secret_id" {
  description = "Secret Manager secret ID holding the HS256 session-token signing key (coach-web auth, issue #68)."
  type        = string
  default     = "auth-session-secret"
}

variable "google_oauth_client_secret_id" {
  description = "Secret Manager secret ID holding the Google OAuth client secret for the OIDC code exchange (coach-web auth, issue #68)."
  type        = string
  default     = "google-oauth-client-secret"
}

# ---------------------------------------------------------------------------
# Non-secret application configuration (cloud-specific only — review PR #93)
# ---------------------------------------------------------------------------

variable "cors_origins" {
  description = "CORS_ORIGINS env for the app — a JSON array string of allowed browser origins. Empty default: the UI is served same-origin by the FastAPI app, so the cloud deployment needs no CORS. Local dev overrides via compose/.env; set here only for a cross-origin consumer."
  type        = string
  default     = ""

  # Carried review item #4 (from #66): fail malformed CORS JSON at `tofu
  # plan`/`apply` time, not at container startup (the app parses CORS_ORIGINS
  # as JSON — a malformed value would crash the container). OpenTofu 1.12
  # `tofu validate` does not evaluate variable validation blocks.
  validation {
    condition     = var.cors_origins == "" || can(jsondecode(var.cors_origins))
    error_message = "cors_origins must be empty or valid JSON (e.g. a JSON array of origin strings, see Settings.CORS_ORIGINS)."
  }
}

variable "auth_whitelist_emails" {
  # ADR-04 (issue #68): the email whitelist IS the access-control boundary, so
  # it is wired explicitly into the service spec rather than left to the app
  # default. The default value is a REAL email by design: it is the owner's
  # public identity as the single user of a single-user personal app, not a
  # credential — access requires a Google-verified ID token for that address,
  # not knowledge of it — so committing it is acceptable (unlike secret
  # material, which never passes through IaC or state).
  description = "AUTH_WHITELIST_EMAILS env for the app — a JSON array string of Google account emails allowed past the auth boundary (ADR-04, issue #65/#68). Single-user personal app: the default is the owner's own address."
  type        = string
  default     = "[\"frederic.pitteloud@gmail.com\"]"

  # Same plan-time JSON gate as cors_origins (carried review item #4 from
  # #66): pydantic-settings parses list[str] fields as JSON, so a malformed
  # value would crash the container at startup — fail at plan instead.
  # OpenTofu 1.12 `tofu validate` does not evaluate variable validation blocks.
  validation {
    condition     = can(jsondecode(var.auth_whitelist_emails))
    error_message = "auth_whitelist_emails must be a valid JSON array string of email addresses (e.g. [\"you@example.com\"]) — pydantic-settings parses it as JSON (same contract as CORS_ORIGINS, issue #93)."
  }
}

# ---------------------------------------------------------------------------
# Remote state (issue #67 — CI deployer state access)
# ---------------------------------------------------------------------------

variable "state_bucket_name" {
  description = "Name of the GCS state bucket. MUST match backend.tf and infra/bootstrap (backend blocks cannot use variables); used here only to scope the CI deployer's state access to the bucket (least privilege, issue #67)."
  type        = string
  default     = "fpittelo-coach-web-tofu-state-europe-west6"
}

# ---------------------------------------------------------------------------
# Networking foundation (direct VPC egress — optional)
# ---------------------------------------------------------------------------

variable "vpc_name" {
  description = "Name of the custom-mode VPC foundation network."
  type        = string
  default     = "coach-web-vpc"
}

variable "subnet_name" {
  description = "Name of the europe-west6 subnet."
  type        = string
  default     = "coach-web-subnet"
}

variable "subnet_cidr" {
  description = "CIDR range of the europe-west6 subnet."
  type        = string
  default     = "10.60.0.0/24"
}

variable "enable_vpc_egress" {
  description = "Attach direct VPC egress to the Cloud Run service. Default false — the stack calls public endpoints only (OpenRouter, Intervals.icu, GitHub)."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Google Identity OIDC — config surface (whitelist enforcement is issue #65)
# ---------------------------------------------------------------------------

variable "oidc_client_id" {
  # ADR-04 (issue #68): AUTH_ENABLED is fixed to "true" for the Cloud Run
  # service and the email whitelist IS the access-control boundary, so the
  # client ID is REQUIRED — an empty value must fail at `tofu plan`/`apply`
  # time, never at container boot (the app's middleware fails closed with
  # AuthConfigError at startup instead). The full-shape regex (numeric prefix,
  # lowercase alphanumeric suffix) rejects placeholder values that would pass
  # a bare ".apps.googleusercontent.com" suffix check.
  description = "Google OAuth web client ID (audience the app validates). Required — create the OAuth web client once in the Google Cloud Console, then set repo variable OIDC_CLIENT_ID (CI deploys) or oidc_client_id in terraform.tfvars (local applies)."
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^[0-9]+-[a-z0-9]+\\.apps\\.googleusercontent\\.com$", var.oidc_client_id))
    error_message = "oidc_client_id must be a Google OAuth web client ID of the form <number>-<lowercase-alphanumeric>.apps.googleusercontent.com. Set repo variable OIDC_CLIENT_ID (CI deploys) or oidc_client_id in terraform.tfvars (local applies); the client is created once in the Google Cloud Console."
  }
}

# ---------------------------------------------------------------------------
# Workload Identity Federation — keyless CI (ADR-06, consumed by issue #67)
# ---------------------------------------------------------------------------

variable "github_org" {
  description = "GitHub organization/user owning the repository allowed to deploy."
  type        = string
  default     = "fpittelo"
}

variable "github_repo" {
  description = "GitHub repository allowed to deploy (per-repo WIF attribute condition, least privilege). This is the IaC repository itself — NOT the training-plan repo (github_plan_repo)."
  type        = string
  default     = "coach-web"
}

variable "wif_pool_id" {
  description = "Workload Identity Pool ID for GitHub Actions federation."
  type        = string
  default     = "github-actions-pool"
}

variable "wif_provider_id" {
  description = "Workload Identity Pool Provider ID (GitHub OIDC)."
  type        = string
  default     = "github-actions-provider"
}

variable "deployer_sa_id" {
  description = "Service account ID for the keyless CI deployer."
  type        = string
  default     = "gha-coach-web-deployer"
}
