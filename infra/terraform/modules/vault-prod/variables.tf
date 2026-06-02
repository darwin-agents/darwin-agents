/**
 * vault-prod — HashiCorp Vault in HA raft mode on Kubernetes.
 *
 * No cloud KMS (Hetzner has none), so there is NO seal stanza: Vault boots
 * SEALED and an operator runs `vault operator init` + `vault operator unseal`
 * once (and again after every pod restart). See the env's README runbook.
 *
 * Because raft pods stay NotReady until unsealed, the Helm release uses
 * wait=false / atomic=false — Terraform must NOT block on readiness.
 */

variable "namespace" {
  type        = string
  description = "Namespace for the Vault release."
  default     = "vault"
}

variable "chart_version" {
  type        = string
  description = "hashicorp/vault Helm chart version (0.32.0 bundles Vault 1.21.2)."
  default     = "0.32.0"
}

variable "vault_image_tag" {
  type        = string
  description = "Vault application image tag."
  default     = "1.21.2"
}

variable "replicas" {
  type        = number
  description = "Raft replica count (odd number for quorum)."
  default     = 3
}

variable "storage_class" {
  type        = string
  description = "StorageClass for the raft data + audit PVCs (e.g. hcloud-volumes). null = cluster default (k3s has none when local-storage is disabled, so set this for real use)."
  default     = null
}

variable "data_storage_size" {
  type        = string
  description = "PVC size for the raft data volume on each replica."
  default     = "10Gi"
}

variable "audit_storage_size" {
  type        = string
  description = "PVC size for the audit-log volume on each replica."
  default     = "10Gi"
}
