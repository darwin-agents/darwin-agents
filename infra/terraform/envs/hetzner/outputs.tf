output "kubeconfig_path" {
  value       = module.cluster.kubeconfig_path
  description = "Path to the cluster kubeconfig. Use as KUBECONFIG for kubectl."
}

output "control_plane_ipv4" {
  value       = module.cluster.control_plane_ipv4
  description = "Public IPv4 of the control-plane (SSH here; or point DNS here if not using a separate LB IP)."
}

output "platform_url" {
  value       = "https://${var.domain}"
  description = "Public platform API URL (once DNS + TLS are live)."
}

output "vault_address" {
  value       = module.platform_stack.vault_address
  description = "In-cluster Vault address (raft; unseal + bootstrap via the README runbook)."
}

output "next_steps" {
  value       = <<-EOT
    1. Point ${var.domain} (A record) at the ingress LoadBalancer IP:
         KUBECONFIG=${module.cluster.kubeconfig_path} kubectl -n ingress-nginx get svc ingress-nginx-controller
    2. Initialise + unseal Vault and wire Kubernetes auth (see README.md "Vault runbook").
    3. Verify gVisor: kubectl get runtimeclass gvisor; spawn a darwin and confirm its pod runs.
  EOT
  description = "Manual post-apply steps."
}
