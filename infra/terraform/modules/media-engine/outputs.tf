output "release_name" {
  value       = helm_release.media_engine.name
  description = "Name of the deployed Helm release."
}

output "namespace" {
  value       = helm_release.media_engine.namespace
  description = "Kubernetes namespace of the release."
}

output "service_url" {
  value       = "http://${helm_release.media_engine.name}.${helm_release.media_engine.namespace}.svc.cluster.local:8000"
  description = "In-cluster URL of the engine's REST API."
}
