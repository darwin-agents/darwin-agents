/**
 * k8s-addons — cluster ingress + the platform Ingress resource.
 *
 * This is the provider-agnostic "how does the API get exposed" layer,
 * factored out of the darwin-platform module so the same supervisor
 * release can be fronted differently per environment:
 *
 *   exposure = "kind-hostport"  → ingress-nginx as a hostNetwork pod on
 *                                 the control-plane node (kind maps host
 *                                 port → 80). Local dev.
 *   exposure = "loadbalancer"   → ingress-nginx behind a Service of
 *                                 type=LoadBalancer (needs a cloud CCM,
 *                                 e.g. hcloud-cloud-controller-manager).
 *   exposure = "none"           → install nothing; the caller fronts the
 *                                 Service some other way.
 */

variable "namespace" {
  type        = string
  description = "Namespace the platform Service (the Ingress backend) lives in."
}

variable "service_name" {
  type        = string
  description = "Name of the platform Service to route to (the chart names it after the release: darwin-platform)."
}

variable "service_port" {
  type        = number
  description = "Port the platform Service answers on."
  default     = 8000
}

variable "exposure" {
  type        = string
  description = "How to expose the API: kind-hostport | loadbalancer | none."
  default     = "kind-hostport"

  validation {
    condition     = contains(["kind-hostport", "loadbalancer", "none"], var.exposure)
    error_message = "exposure must be one of: kind-hostport, loadbalancer, none."
  }
}

variable "ingress_nginx_chart_version" {
  type        = string
  description = "ingress-nginx Helm chart version."
  default     = "4.11.2"
}

variable "ingress_class_name" {
  type        = string
  description = "IngressClass the platform Ingress binds to."
  default     = "nginx"
}

variable "ingress_host" {
  type        = string
  description = "Optional host the Ingress matches on (e.g. api.darwin.example.com). Empty = match all hosts (the kind/local pattern)."
  default     = ""
}

variable "proxy_read_timeout" {
  type        = number
  description = "nginx proxy-read-timeout (seconds). The dashboard's WS stream stays open, so keep this high."
  default     = 300
}

variable "proxy_send_timeout" {
  type        = number
  description = "nginx proxy-send-timeout (seconds)."
  default     = 300
}

# -- TLS / cert-manager (cloud) ---------------------------------------------

variable "tls_enabled" {
  type        = bool
  description = "Terminate TLS at the Ingress. Requires either a cert-manager ClusterIssuer (set cluster_issuer) or a pre-provisioned tls_secret_name."
  default     = false
}

variable "cluster_issuer" {
  type        = string
  description = "cert-manager ClusterIssuer to annotate the Ingress with (e.g. letsencrypt-prod). Empty = no cert-manager annotation (use a pre-provisioned tls_secret_name instead)."
  default     = ""
}

variable "tls_secret_name" {
  type        = string
  description = "Secret holding the TLS cert/key for ingress_host. With cert-manager + cluster_issuer set, the secret is created automatically under this name; otherwise it must already exist."
  default     = ""
}

# -- LoadBalancer exposure (Hetzner) ----------------------------------------

variable "lb_name" {
  type        = string
  description = "Name annotation for the cloud LoadBalancer (load-balancer.hetzner.cloud/name). Unique per LB."
  default     = "darwin-ingress"
}

variable "lb_location" {
  type        = string
  description = "Hetzner location for the LoadBalancer (load-balancer.hetzner.cloud/location)."
  default     = "nbg1"
}

variable "lb_type" {
  type        = string
  description = "Hetzner LoadBalancer type (lb11/lb21/lb31)."
  default     = "lb11"
}

# -- cert-manager -----------------------------------------------------------

variable "cert_manager_install" {
  type        = bool
  description = "Install cert-manager + a Let's-Encrypt ClusterIssuer (named by cluster_issuer)."
  default     = false
}

variable "cert_manager_chart_version" {
  type        = string
  description = "cert-manager Helm chart version."
  default     = "v1.20.2"
}

variable "acme_email" {
  type        = string
  description = "Contact email for the ACME (Let's Encrypt) account."
  default     = ""
}

variable "acme_server" {
  type        = string
  description = "ACME directory URL. Default is Let's Encrypt PROD; use the staging URL while testing to avoid rate limits."
  default     = "https://acme-v02.api.letsencrypt.org/directory"
}
