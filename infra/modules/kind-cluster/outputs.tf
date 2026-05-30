output "kubeconfig_path" {
  value       = kind_cluster.this.kubeconfig_path
  description = "Path to the kubeconfig file kind wrote."
}

output "cluster_name" {
  value       = kind_cluster.this.name
  description = "kind cluster name (also kubeconfig context name)."
}

output "endpoint" {
  value       = kind_cluster.this.endpoint
  description = "Internal API server URL. Tests + scripts should generally read kubeconfig_path instead."
}
