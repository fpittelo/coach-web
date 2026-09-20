# Remote state — GCS bucket in europe-west6 (Swiss data residency, AC3).
#
# The GCS backend provides native state locking (lock objects) and the bucket
# created by infra/bootstrap enables object versioning, so every state
# transition is auditable and recoverable.
#
# Chicken-and-egg: the state bucket cannot create itself. It is provisioned by
# the separate `infra/bootstrap` configuration (local backend) — see README.md.
#
# Backend blocks cannot reference variables: the literal bucket name below
# MUST match `state_bucket_name` in infra/bootstrap. Change both together.

terraform {
  backend "gcs" {
    bucket = "fpittelo-coach-web-tofu-state-europe-west6"
    prefix = "coach-web/infra"
  }
}
