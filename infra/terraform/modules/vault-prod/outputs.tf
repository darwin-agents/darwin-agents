output "namespace" {
  value       = var.namespace
  description = "Namespace Vault runs in."
}

output "address" {
  value       = "http://vault.${var.namespace}.svc.cluster.local:8200"
  description = "In-cluster Vault address. Unsealed + bootstrapped via the runbook before darwins can seal keys."
}
