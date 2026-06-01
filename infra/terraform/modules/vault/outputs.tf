output "namespace" {
  value       = var.namespace
  description = "Namespace Vault runs in."
}

output "address" {
  value       = "http://vault.${var.namespace}.svc.cluster.local:8200"
  description = "In-cluster Vault API endpoint. The platform mounts this as VAULT_ADDR."
}

output "root_token" {
  value       = random_password.root_token.result
  sensitive   = true
  description = "Vault root token. Marked sensitive — Terraform won't print it on apply. Reach it via `terraform output -raw vault_root_token` from the env directory."
}
