output "namespace" {
  value       = var.namespace
  description = "Namespace the supervisor runs in."
}

output "service_name" {
  # The chart names its Service (and ServiceAccount) after the release.
  value       = local.release_name
  description = "Kubernetes Service the supervisor's HTTP API answers on."
  depends_on  = [helm_release.platform]
}

output "service_account" {
  value       = local.release_name
  description = "ServiceAccount the supervisor pod runs as (carries the K8s-driver RBAC)."
  depends_on  = [helm_release.platform]
}
