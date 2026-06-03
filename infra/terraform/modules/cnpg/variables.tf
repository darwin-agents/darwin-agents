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

variable "storage_class" {
  type        = string
  description = "StorageClass for the Postgres PVCs. null = the cluster's default StorageClass (kind's `standard`). Cloud envs set this to a block-storage class (e.g. hcloud-volumes)."
  default     = null
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

# -- Point-in-time recovery (doc 20: PITR + backup) -------------------------
# CNPG continuously archives WAL + takes base backups to an S3-compatible
# object store (barmanObjectStore). Together they give point-in-time
# recovery: restore the latest base backup, then replay WAL up to any
# chosen timestamp. Default OFF so local/kind (no object store) is
# unaffected; cloud envs flip `backup_enabled` and supply the destination.
variable "backup_enabled" {
  type        = bool
  description = "Enable continuous WAL archiving + scheduled base backups to object storage (PITR). Requires backup_destination_path + a credentials Secret."
  default     = false
}

variable "backup_destination_path" {
  type        = string
  description = "S3-style destination, e.g. s3://my-bucket/darwin-pg. Required when backup_enabled."
  default     = ""
}

variable "backup_endpoint_url" {
  type        = string
  description = "Object-store endpoint URL (e.g. https://fsn1.your-objectstorage.com for Hetzner, https://s3.amazonaws.com for AWS). Empty = the provider default for AWS S3."
  default     = ""
}

variable "backup_s3_credentials_secret" {
  type        = string
  description = "Name of a Kubernetes Secret (in this namespace) with keys ACCESS_KEY_ID and ACCESS_SECRET_KEY for the object store. Required when backup_enabled."
  default     = ""
}

variable "backup_retention_policy" {
  type        = string
  description = "How long to keep base backups + WAL (CNPG retention syntax, e.g. 30d, 4w). Older backups are pruned automatically."
  default     = "30d"
}

variable "backup_wal_compression" {
  type        = string
  description = "WAL compression algorithm: gzip | bzip2 | snappy. Cuts archive size + egress."
  default     = "gzip"
}

variable "backup_schedule" {
  type        = string
  description = "Cron schedule for base backups (CNPG uses a 6-field cron WITH seconds). Default: 02:30 UTC daily."
  default     = "0 30 2 * * *"
}
