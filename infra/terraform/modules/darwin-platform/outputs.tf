output "namespace" {
  value       = var.namespace
  description = "Namespace the supervisor runs in."
}

output "service_name" {
  value       = kubernetes_service.platform.metadata[0].name
  description = "Kubernetes Service the supervisor's HTTP API answers on."
}

output "service_account" {
  value       = kubernetes_service_account.platform.metadata[0].name
  description = "ServiceAccount the supervisor pod runs as (carries the K8s-driver RBAC)."
}
