variable "project_id" {
  description = "GCP project ID hosting the coach-web stack."
  type        = string
}

variable "region" {
  description = "GCP region for the subnet (europe-west6, Zürich)."
  type        = string
}

variable "vpc_name" {
  description = "Name of the custom-mode VPC."
  type        = string
}

variable "subnet_name" {
  description = "Name of the regional subnet."
  type        = string
}

variable "subnet_cidr" {
  description = "CIDR range of the regional subnet."
  type        = string
}
