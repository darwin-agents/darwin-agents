variable "namespace" {
  type        = string
  description = "Namespace Vault runs in."
  default     = "vault"
}

variable "chart_version" {
  type        = string
  description = "Pinned hashicorp/vault Helm chart version."
  default     = "0.28.1"
}
