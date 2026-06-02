/**
 * platform-stack — the cloud-agnostic Darwin platform composition.
 *
 * Everything that is the *same* on every provider lives here: the
 * Postgres (CNPG), Vault, LiteLLM and supervisor (darwin-platform)
 * releases plus the ingress addon. A provider env (envs/local,
 * envs/hetzner, …) supplies a cluster + configured providers and calls
 * this module once with provider-specific knobs.
 *
 * Providers (helm/kubernetes/kubectl) are inherited from the calling
 * env's default provider configuration — this module declares no
 * provider blocks.
 */

# -- placement --------------------------------------------------------------

variable "namespace" {
  type        = string
  description = "Namespace the supervisor + Postgres Cluster run in. Per-Darwin namespaces (darwin-<id>) are created at runtime by the K8s driver."
  default     = "darwin"
}

variable "vault_namespace" {
  type        = string
  description = "Namespace for the Vault release."
  default     = "vault"
}

variable "litellm_namespace" {
  type        = string
  description = "Namespace for the LiteLLM proxy."
  default     = "litellm"
}

variable "cnpg_operator_namespace" {
  type        = string
  description = "Namespace for the CloudNativePG operator (separate from the data plane)."
  default     = "cnpg-system"
}

# -- images -----------------------------------------------------------------

variable "platform_image" {
  type        = string
  description = "Supervisor image (repo:tag). Built locally as darwin-platform:dev, or a GHCR ref in cloud."
  default     = "darwin-platform:dev"
}

variable "orchestrator_image" {
  type        = string
  description = "Image the K8s driver uses for per-Darwin orchestrator pods. Defaults to the supervisor image so one build covers both."
  default     = "darwin-platform:dev"
}

variable "image_pull_policy" {
  type        = string
  description = "imagePullPolicy for the supervisor. IfNotPresent + a local docker build lets kind use the local image; Always pulls from a registry."
  default     = "IfNotPresent"
}

# -- isolation --------------------------------------------------------------

variable "runtime_class" {
  type        = string
  description = "RuntimeClass for per-Darwin orchestrator pods. kind: a runc-backed shim named `kata`; cloud: `gvisor` (runsc on standard VMs)."
  default     = "kata"
}

variable "create_runtime_class" {
  type        = bool
  description = "If true, the chart creates the RuntimeClass resource. false when the env installs it out-of-band (kind shim, or cloud node bootstrap)."
  default     = false
}

# -- storage ----------------------------------------------------------------

variable "storage_class" {
  type        = string
  description = "StorageClass for the Postgres PVCs. null = cluster default (kind `standard`); cloud sets a block-storage class (e.g. hcloud-volumes)."
  default     = null
}

variable "database_storage_size" {
  type        = string
  description = "PVC size per Postgres instance."
  default     = "2Gi"
}

variable "database_instances" {
  type        = number
  description = "Postgres instance count. 1 for local; >=3 for HA in cloud."
  default     = 1
}

# -- LiteLLM ----------------------------------------------------------------

variable "litellm_master_key" {
  type        = string
  description = "LiteLLM proxy admin key. Stays in-cluster; forwarded to per-Darwin pods as DARWIN_POD_LITELLM_KEY."
  sensitive   = true
}

variable "litellm_provider_keys" {
  type        = map(string)
  description = "Upstream LLM provider keys (OPENAI_API_KEY, ANTHROPIC_API_KEY, …) LiteLLM proxies. Empty = installs but every call 401s until populated."
  sensitive   = true
  default     = {}
}

variable "litellm_config_yaml" {
  type        = string
  description = "Full LiteLLM config.yaml content (the env reads the file and passes the string so this module stays filesystem-agnostic)."
}

# -- supervisor runtime -----------------------------------------------------

variable "wallet_provider" {
  type        = string
  description = "Wallet provider for spawned Darwins: cdp | local-vault | web3. Forwarded into DarwinPodConfig (the supervisor itself holds no wallet in k8s mode)."
  default     = "cdp"
}

variable "heartbeat_seconds" {
  type        = number
  description = "Supervisor heartbeat interval."
  default     = 5
}

variable "extra_args" {
  type        = list(string)
  description = "Extra `darwin-platform serve` flags (e.g. --cors-origin, --auth-tokens)."
  default     = []
}

# -- exposure (ingress addon) ----------------------------------------------

variable "exposure" {
  type        = string
  description = "API exposure: kind-hostport | loadbalancer | none."
  default     = "kind-hostport"
}

variable "service_port" {
  type        = number
  description = "Port the supervisor Service answers on."
  default     = 8000
}

variable "ingress_host" {
  type        = string
  description = "Optional host the Ingress matches (real domain in cloud). Empty = match all (kind/local)."
  default     = ""
}

variable "tls_enabled" {
  type        = bool
  description = "Terminate TLS at the Ingress (cloud)."
  default     = false
}

variable "cluster_issuer" {
  type        = string
  description = "cert-manager ClusterIssuer for the Ingress cert (e.g. letsencrypt-prod)."
  default     = ""
}

variable "tls_secret_name" {
  type        = string
  description = "TLS Secret name for ingress_host (cert-manager fills it when cluster_issuer is set)."
  default     = ""
}
