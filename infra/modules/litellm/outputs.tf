output "namespace" {
  value       = var.namespace
  description = "Namespace LiteLLM runs in."
}

output "service_name" {
  value       = kubernetes_service.this.metadata[0].name
  description = "Kubernetes Service name. The platform points --litellm-url at http://<service>.<namespace>.svc.cluster.local:4000."
}

output "in_cluster_url" {
  value       = "http://${kubernetes_service.this.metadata[0].name}.${var.namespace}.svc.cluster.local:4000"
  description = "Fully-qualified in-cluster URL the platform mounts as --litellm-url."
}
