variable "cluster_name" {
  type        = string
  description = "kind cluster name (also used as the kubeconfig context name)."
}

variable "kubeconfig_path" {
  type        = string
  description = "Absolute path where the cluster's kubeconfig should be written."
}

variable "node_image" {
  type        = string
  description = "Image used for every kind node. Pin so a rerun lands on the same K8s version. Bump intentionally."
  default     = "kindest/node:v1.31.0"
}

variable "host_api_port" {
  type        = number
  description = "Port on the host mapped to the cluster's ingress (node port 80)."
  default     = 8000
}

variable "host_extra_port" {
  type        = number
  description = "Optional second host port → cluster ingress port 443. Useful for serving TLS off a local mkcert cert."
  default     = 8443
}
