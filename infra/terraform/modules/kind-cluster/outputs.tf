output "kubeconfig_path" {
  value       = var.kubeconfig_path
  description = "Absolute path of the kubeconfig file kind wrote. The Makefile + manual kubectl use it directly; downstream Terraform providers use the inline credentials below to dodge the eager-config-validation problem."

  depends_on = [null_resource.cluster]
}

output "cluster_name" {
  value       = var.cluster_name
  description = "kind cluster name. The kubeconfig context is named 'kind-<cluster_name>'."
  depends_on  = [null_resource.cluster]
}

output "endpoint" {
  value       = data.external.credentials.result.host
  description = "API server URL for inline provider config."
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
