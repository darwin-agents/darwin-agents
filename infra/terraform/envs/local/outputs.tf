output "kubeconfig_path" {
  value       = module.cluster.kubeconfig_path
  description = "Absolute path to the kind cluster's kubeconfig file. Use it as KUBECONFIG for kubectl."
}

output "platform_url" {
  value       = "http://localhost:${var.host_api_port}"
  description = "Where the platform API answers on the host (mapped through kind's extraPortMappings → in-cluster Service)."
}

output "vault_address" {
  value       = module.platform_stack.vault_address
  description = "In-cluster Vault address (only reachable from inside the cluster; use kubectl port-forward to reach from your laptop)."
}

output "platform_namespace" {
  value       = var.platform_namespace
  description = "Namespace the darwin-platform supervisor runs in. Per-Darwin namespaces (darwin-<id>) are created at runtime."
}
