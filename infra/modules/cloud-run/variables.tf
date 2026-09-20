variable "project_id" {
  description = "GCP project ID hosting the coach-web stack."
  type        = string
}

variable "region" {
  description = "GCP region for the Cloud Run service (europe-west6, Zürich)."
  type        = string
}

variable "service_name" {
  description = "Cloud Run service name."
  type        = string
}

# ---------------------------------------------------------------------------
# Container images (AC3) — GHCR, pinned tags, wired by the deploy workflow #67
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Per-container resources — sized for scale-to-zero ($0 at idle, ADR-04)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Scaling (ADR-04: scale-to-zero)
# ---------------------------------------------------------------------------

variable "min_instance_count" {
  description = "Minimum instances (0 = scale-to-zero, ADR-04)."
  type        = number
}

variable "max_instance_count" {
  description = "Maximum instances (cost ceiling)."
  type        = number
}

# ---------------------------------------------------------------------------
# Invocation & identity
# ---------------------------------------------------------------------------

variable "allow_unauthenticated" {
  description = "Grant roles/run.invoker to allUsers on this service (ADR-04 app-level OIDC)."
  type        = bool
}

variable "runtime_sa_id" {
  description = "Service account ID for the Cloud Run runtime identity."
  type        = string
}

variable "oidc_client_id" {
  # ADR-04 (issue #68): the app-level Google OIDC whitelist IS the
  # access-control boundary, and AUTH_ENABLED is fixed to "true" for this
  # service — so a missing client ID must fail at `tofu plan`/`apply` time,
  # never at container boot (the app's middleware fails closed with
  # AuthConfigError at startup instead). The full-shape regex (numeric prefix,
  # lowercase alphanumeric suffix) rejects placeholder values that would pass
  # a bare ".apps.googleusercontent.com" suffix check.
  description = "Google OAuth web client ID injected as GOOGLE_OIDC_CLIENT_ID (issue #65/#68). Required — obtain it once from the Google Cloud Console and set repo variable OIDC_CLIENT_ID (CI) or oidc_client_id in terraform.tfvars (local applies)."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+-[a-z0-9]+\\.apps\\.googleusercontent\\.com$", var.oidc_client_id))
    error_message = "oidc_client_id must be a Google OAuth web client ID of the form <number>-<lowercase-alphanumeric>.apps.googleusercontent.com. Set repo variable OIDC_CLIENT_ID (CI deploys) or oidc_client_id in terraform.tfvars (local applies); the client is created once in the Google Cloud Console."
  }
}

variable "oidc_issuer_uri" {
  description = "Google OIDC issuer URI injected as GOOGLE_OIDC_ISSUER."
  type        = string
}

# ---------------------------------------------------------------------------
# Secret Manager (AC4) — names only; VERSIONS are populated out-of-band
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
  # default — the deployed posture must be visible in IaC and the Cloud Run
  # console, not implied. The default value is a REAL email by design: it is
  # the owner's public identity as the single user of a single-user personal
  # app, it is not a credential (access requires a Google-verified ID token
  # for that address, not knowledge of it), and it is by nature public config
  # — acceptable to commit, unlike secret material.
  description = "AUTH_WHITELIST_EMAILS env for the app — a JSON array string of Google account emails allowed past the auth boundary (ADR-04, issue #65/#68). Single-user personal app: the default is the owner's own address."
  type        = string
  default     = "[\"frederic.pitteloud@gmail.com\"]"

  # Same plan-time JSON gate as cors_origins (carried review item #4 from
  # #66): pydantic-settings parses list[str] fields as JSON, so a malformed
  # value would crash the container at startup — fail at plan instead.
  # OpenTofu 1.12 `tofu validate` does not evaluate validation blocks.
  validation {
    condition     = can(jsondecode(var.auth_whitelist_emails))
    error_message = "auth_whitelist_emails must be a valid JSON array string of email addresses (e.g. [\"you@example.com\"]) — pydantic-settings parses it as JSON (same contract as CORS_ORIGINS, issue #93)."
  }
}

# ---------------------------------------------------------------------------
# Networking (direct VPC egress — optional, retained from #64)
# ---------------------------------------------------------------------------

variable "enable_vpc_egress" {
  description = "Attach direct VPC egress to the service (the stack calls public endpoints only; disabled by default)."
  type        = bool
}

variable "subnet_self_link" {
  description = "Self-link of the europe-west6 subnet used for direct VPC egress."
  type        = string

  validation {
    condition     = !var.enable_vpc_egress || var.subnet_self_link != ""
    error_message = "enable_vpc_egress = true requires a non-empty subnet_self_link."
  }
}
