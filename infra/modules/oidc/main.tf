# Google Identity OIDC — configuration surface only (ADR-04).
#
# Issue #64 scope: expose the client configuration the app will use to validate
# Google ID tokens. The OAuth consent screen + web client ID are created once
# in the Google Cloud Console (no clean IaC resource exists for OAuth web
# clients); the application-level whitelist that consumes this config is
# issue #65.

locals {
  # Google's public OIDC issuer — fixed by Google, not a variable.
  issuer_uri = "https://accounts.google.com"
}

output "client_id" {
  description = "Google OAuth web client ID (audience the app expects). Empty until configured in the Console."
  value       = var.oidc_client_id
}

output "issuer_uri" {
  description = "Google OIDC issuer URL."
  value       = local.issuer_uri
}

output "configured" {
  description = "True once a real client ID has been provided (guards #65 onboarding)."
  value       = var.oidc_client_id != ""
}
