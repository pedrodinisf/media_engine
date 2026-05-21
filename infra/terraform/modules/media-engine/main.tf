# media_engine — Terraform module skeleton.
#
# Wraps a Helm release of the bundled chart. Operators provide their
# own kubernetes/helm provider configuration upstream; this module only
# declares the chart input wiring.

terraform {
  required_version = ">= 1.5"

  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = ">= 2.12"
    }
  }
}

resource "helm_release" "media_engine" {
  name      = var.release_name
  namespace = var.namespace
  chart     = "${path.module}/../../helm/media-engine"

  values = [
    yamlencode({
      replicaCount = var.replicas
      image = {
        repository = var.image_repository
        tag        = var.image_tag
      }
      db = {
        url = var.db_url
      }
      storage = {
        permanentStore = "/var/lib/media_engine"
        pvc = {
          enabled = true
          size    = var.storage_size
        }
      }
      # Annotate the release with the cluster identifier so dashboards
      # / drift detection can attribute it without inspecting the
      # provider's kube context.
      config = {
        extraEnv = var.cluster == "" ? {} : {
          MEDIA_ENGINE_CLUSTER_LABEL = var.cluster
        }
      }
    }),
  ]
}
