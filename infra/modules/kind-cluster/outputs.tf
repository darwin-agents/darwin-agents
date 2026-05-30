output "kubeconfig_path" {
  value       = kind_cluster.this.kubeconfig_path
  description = "Path to the kubeconfig file kind wrote. Use this for kubectl + the Makefile; provider configs use the inline credentials below to avoid the 'file doesn't exist at plan time' bootstrap problem."
}

output "cluster_name" {
  value       = kind_cluster.this.name
  description = "kind cluster name (also kubeconfig context name)."
}

output "endpoint" {
  value       = kind_cluster.this.endpoint
  description = "API server URL. Use this in inline provider config."
}

output "client_certificate" {
  value       = kind_cluster.this.client_certificate
  description = "Base64-decoded PEM client certificate."
  sensitive   = true
}

output "client_key" {
  value       = kind_cluster.this.client_key
  description = "Base64-decoded PEM client private key."
  sensitive   = true
}

output "cluster_ca_certificate" {
  value       = kind_cluster.this.cluster_ca_certificate
  description = "Base64-decoded PEM cluster CA certificate."
  sensitive   = true
}
