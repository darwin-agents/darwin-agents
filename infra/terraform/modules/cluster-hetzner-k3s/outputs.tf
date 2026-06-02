# MIRRORS modules/kind-cluster/outputs.tf so any env wires the
# helm/kubernetes/kubectl providers identically regardless of provider.

output "kubeconfig_path" {
  value       = var.kubeconfig_path
  description = "Absolute path of the fetched + rewritten kubeconfig."
  depends_on  = [null_resource.fetch_kubeconfig]
}

output "cluster_name" {
  value       = var.cluster_name
  description = "Cluster name; the kubeconfig context is 'default' (k3s)."
}

output "endpoint" {
  value       = data.external.credentials.result.host
  description = "API server URL (public IP) for inline provider config."
}

output "cluster_ca_certificate" {
  value       = data.external.credentials.result.cluster_ca_certificate
  description = "PEM cluster CA certificate."
  sensitive   = true
}

output "client_certificate" {
  value       = data.external.credentials.result.client_certificate
  description = "PEM client certificate."
  sensitive   = true
}

output "client_key" {
  value       = data.external.credentials.result.client_key
  description = "PEM client private key."
  sensitive   = true
}

# --- extra outputs the Hetzner cloud addons (CCM) need --------------------
output "network_id" {
  value       = hcloud_network.this.id
  description = "Private network ID (for the hcloud CCM secret)."
}

output "network_name" {
  value       = hcloud_network.this.name
  description = "Private network name (the hcloud CCM secret's `network` key)."
}

output "control_plane_ipv4" {
  value       = hcloud_server.control_plane.ipv4_address
  description = "Public IPv4 of the control-plane (point a DNS A record / SSH here)."
}
