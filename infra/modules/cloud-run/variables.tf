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
  description = "Google OAuth web client ID injected as GOOGLE_OIDC_CLIENT_ID (config surface, issue #65)."
  type        = string
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

# ---------------------------------------------------------------------------
# Non-secret application configuration (cloud-specific only — review PR #93)
# ---------------------------------------------------------------------------

variable "cors_origins" {
  description = "CORS_ORIGINS env for the app — a JSON array string of allowed browser origins. Empty default: the UI is served same-origin by the FastAPI app, so the cloud deployment needs no CORS. Local dev overrides via compose/.env; set here only for a cross-origin consumer."
  type        = string
  default     = ""
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
