output "namespace" {
  value       = var.namespace
  description = "Namespace where the Postgres Cluster lives. Must match the platform's namespace so the Secret mount is in-namespace."
}

output "connection_secret_name" {
  # CNPG generates `<cluster_name>-app` with keys: username, password,
  # host, port, dbname, uri, jdbc-uri. The platform module mounts `uri`
  # as DATABASE_URL.
  value       = "${var.cluster_name}-app"
  description = "Name of the Kubernetes Secret CNPG populates with the connection string for the bootstrapped database."
}

output "cluster_name" {
  value       = var.cluster_name
  description = "Name of the Postgres Cluster CR (also used in service names and Secret names)."
}
