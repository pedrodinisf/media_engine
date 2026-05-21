variable "cluster" {
  type        = string
  default     = ""
  description = <<-EOT
    Human-readable cluster identifier (e.g. "prod-us-east-1"). The
    actual kubernetes/helm provider configuration is plumbed in
    upstream — this module just declares the variable so callers can
    pass it through as a tag, an annotation, or a Helm value without
    needing to fork the module. Recorded as the
    ``media-engine/cluster`` label on the release.
  EOT
}

variable "release_name" {
  type        = string
  default     = "media-engine"
  description = "Helm release name."
}

variable "namespace" {
  type        = string
  default     = "media-engine"
  description = "Kubernetes namespace the release lands in."
}

variable "replicas" {
  type        = number
  default     = 1
  description = "Engine pod replica count."
}

variable "image_repository" {
  type        = string
  default     = "ghcr.io/anthropics/media-engine"
  description = "Container image repository."
}

variable "image_tag" {
  type        = string
  default     = "0.1.0"
  description = "Container image tag."
}

variable "db_url" {
  type        = string
  description = "Postgres connection string (postgresql+psycopg://...)."
  sensitive   = true
}

variable "storage_size" {
  type        = string
  default     = "100Gi"
  description = "PVC size for the artifact store."
}
