variable "oidc_client_id" {
  description = "Google OAuth web client ID (audience the app validates). Empty = not yet configured."
  type        = string

  validation {
    condition     = var.oidc_client_id == "" || can(regex("\\.apps\\.googleusercontent\\.com$", var.oidc_client_id))
    error_message = "oidc_client_id must be empty (not yet configured) or a Google OAuth client ID ending in .apps.googleusercontent.com."
  }
}
