output "namespace" {
  value       = var.namespace
  description = "Namespace the supervisor runs in."
}

output "service_name" {
  value       = module.platform.service_name
  description = "Kubernetes Service the supervisor's HTTP API answers on."
}

output "service_account" {
  value       = module.platform.service_account
  description = "ServiceAccount the supervisor pod runs as (carries the K8s-driver RBAC)."
}

output "vault_address" {
  value       = local.vault_address
  description = "In-cluster Vault address (dev or raft, per vault_mode)."
}

output "ingress_enabled" {
  value       = module.addons.enabled
  description = "Whether an ingress controller + Ingress were installed."
}
