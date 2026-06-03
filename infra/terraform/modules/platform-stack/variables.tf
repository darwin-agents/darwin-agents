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

variable "vault_mode" {
  type        = string
  description = "Vault deployment: 'dev' (in-memory, auto-unseal — local) or 'raft' (HA + manual unseal — prod)."
  default     = "dev"

  validation {
    condition     = contains(["dev", "raft"], var.vault_mode)
    error_message = "vault_mode must be 'dev' or 'raft'."
  }
}

variable "vault_token" {
  type        = string
  description = "Static Vault token for the supervisor. Only used in 'dev' is auto-derived; for 'raft' leave empty and wire Kubernetes auth via the runbook."
  sensitive   = true
  default     = ""
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

variable "runtime_handler" {
  type        = string
  description = "CRI handler the created RuntimeClass points at (only used when create_runtime_class=true). gVisor uses 'runsc'; Kata uses 'kata'."
  default     = "runsc"
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

# -- Postgres PITR backup (doc 20) ------------------------------------------
# Forwarded to the cnpg module. Default off so local/kind is unaffected.
variable "database_backup_enabled" {
  type        = bool
  description = "Enable continuous WAL archiving + scheduled base backups to object storage (PITR)."
  default     = false
}

variable "database_backup_destination_path" {
  type        = string
  description = "S3-style backup destination, e.g. s3://bucket/darwin-pg. Required when database_backup_enabled."
  default     = ""
}

variable "database_backup_endpoint_url" {
  type        = string
  description = "Object-store endpoint URL (Hetzner/MinIO/etc). Empty = AWS S3 default."
  default     = ""
}

variable "database_backup_s3_credentials_secret" {
  type        = string
  description = "Secret (in the platform namespace) with keys ACCESS_KEY_ID + ACCESS_SECRET_KEY for the object store."
  default     = ""
}

variable "database_backup_retention_policy" {
  type        = string
  description = "Backup/WAL retention (CNPG syntax, e.g. 30d)."
  default     = "30d"
}

variable "database_backup_schedule" {
  type        = string
  description = "Base-backup cron (6-field, with seconds). Default 02:30 UTC daily."
  default     = "0 30 2 * * *"
}

# -- Vault auto-unseal (doc 20) ---------------------------------------------
# Forwarded to vault-prod (raft mode only). Default empty = Shamir/manual.
variable "vault_seal_config_hcl" {
  type        = string
  description = "Raw `seal \"<type>\" { ... }` HCL to enable Vault auto-unseal. Empty = manual unseal."
  default     = ""
}

variable "vault_seal_env" {
  type = list(object({
    env_name    = string
    secret_name = string
    secret_key  = string
  }))
  description = "Env the Vault seal stanza needs (e.g. VAULT_TOKEN for transit), each from a Secret key."
  default     = []
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

variable "litellm_database_url" {
  type        = string
  description = "Postgres URL backing LiteLLM's virtual-key + spend tables (doc 13). REQUIRED for per-Darwin virtual keys (the supervisor's --litellm-virtual-keys mode). Empty = no DB, shared-master-key mode (dev only). Point at a `litellm` database on the platform's CloudNativePG cluster."
  sensitive   = true
  default     = ""
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

variable "lb_name" {
  type        = string
  description = "Name annotation for the cloud LoadBalancer (exposure=loadbalancer)."
  default     = "darwin-ingress"
}

variable "lb_location" {
  type        = string
  description = "Hetzner location for the cloud LoadBalancer."
  default     = "nbg1"
}

variable "lb_type" {
  type        = string
  description = "Hetzner LoadBalancer type (lb11/lb21/lb31)."
  default     = "lb11"
}

variable "cert_manager_install" {
  type        = bool
  description = "Install cert-manager + a Let's-Encrypt ClusterIssuer (cloud TLS)."
  default     = false
}

variable "acme_email" {
  type        = string
  description = "Contact email for the ACME (Let's Encrypt) account."
  default     = ""
}

variable "acme_server" {
  type        = string
  description = "ACME directory URL (default: Let's Encrypt prod; use staging while testing)."
  default     = "https://acme-v02.api.letsencrypt.org/directory"
}
