variable "namespace" {
  type        = string
  description = "Namespace the Postgres Cluster CR lands in. Must match where the platform pod that consumes the connection secret runs."
}

variable "operator_namespace" {
  type        = string
  description = "Namespace the CloudNativePG operator runs in (separate from the data plane)."
  default     = "cnpg-system"
}

variable "operator_chart_version" {
  type        = string
  description = "Pinned cloudnative-pg Helm chart version. Bump intentionally so a rerun lands the same operator."
  default     = "0.22.1"
}

variable "cluster_name" {
  type        = string
  description = "Name of the Postgres Cluster custom resource."
}

variable "instances" {
  type        = number
  description = "Number of Postgres instances. 1 for local; ≥3 for HA."
  default     = 1
}

variable "storage_size" {
  type        = string
  description = "PVC size for each Postgres instance."
  default     = "2Gi"
}

variable "database_name" {
  type        = string
  description = "Initial database created on bootstrap. The platform connects to this."
  default     = "darwin"
}

variable "owner_role" {
  type        = string
  description = "Postgres role that owns the bootstrapped database. CNPG creates a Secret named <cluster_name>-app with these credentials."
  default     = "darwin"
}
