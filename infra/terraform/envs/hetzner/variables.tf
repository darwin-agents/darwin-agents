variable "hcloud_token" {
  type        = string
  description = "Hetzner Cloud project API token (Read+Write). Provision via the Hetzner console; pass with TF_VAR_hcloud_token, never commit."
  sensitive   = true
}

variable "cluster_name" {
  type        = string
  description = "Cluster name; prefixes hcloud resources."
  default     = "darwin"
}

variable "location" {
  type        = string
  description = "Hetzner location (cax/ARM lives in nbg1/fsn1/hel1)."
  default     = "nbg1"
}

variable "server_type" {
  type        = string
  description = "Hetzner server type. cax21 = ARM64, 4 vCPU / 8GB."
  default     = "cax21"
}

variable "agent_count" {
  type        = number
  description = "Number of k3s worker nodes."
  default     = 2
}

variable "domain" {
  type        = string
  description = "Public API domain (e.g. api.darwinagents.com). Point an A record at the cluster's control-plane / LoadBalancer IP before applying so HTTP-01 TLS can issue."
}

variable "acme_email" {
  type        = string
  description = "Contact email for the Let's Encrypt ACME account."
}

variable "platform_image" {
  type        = string
  description = "Supervisor image (GHCR ref, pinned by sha- tag in CI)."
  default     = "ghcr.io/darwin-agents/darwin-agents/darwin-platform:latest"
}

variable "wallet_provider" {
  type        = string
  description = "Wallet provider: cdp (the production guard requires it; CDP keys come via secret_env)."
  default     = "cdp"
}

variable "allowed_admin_cidrs" {
  type        = list(string)
  description = "Source CIDRs allowed to reach SSH (22) + kube API (6443), e.g. your office IP /32. Required — there is no open default."

  validation {
    condition     = length(var.allowed_admin_cidrs) > 0 && !contains(var.allowed_admin_cidrs, "0.0.0.0/0")
    error_message = "allowed_admin_cidrs must list specific CIDRs, not 0.0.0.0/0."
  }
}

variable "ssh_public_key" {
  type        = string
  description = "Public SSH key contents authorized on every node."
}

variable "ssh_private_key" {
  type        = string
  description = "Matching private SSH key contents (used to fetch the kubeconfig). Pass via TF_VAR_ssh_private_key."
  sensitive   = true
}

variable "litellm_master_key" {
  type        = string
  description = "LiteLLM proxy admin key (the supervisor mints per-Darwin virtual keys with it). Pass via TF_VAR_litellm_master_key."
  sensitive   = true
}

variable "litellm_provider_keys" {
  type        = map(string)
  description = "Upstream LLM provider keys (CLAUDE_API_KEY, OPENAI_API_KEY, ...) LiteLLM proxies."
  sensitive   = true
  default     = {}
}

variable "litellm_database_url" {
  type        = string
  description = "Postgres URL backing LiteLLM virtual keys + spend (doc 13). Required for per-Darwin virtual keys; point at a `litellm` database on the CNPG cluster. Empty = shared-master-key mode."
  sensitive   = true
  default     = ""
}

variable "acme_server" {
  type        = string
  description = "ACME directory. Default Let's Encrypt PROD; switch to staging (https://acme-staging-v02.api.letsencrypt.org/directory) while validating to avoid rate limits."
  default     = "https://acme-v02.api.letsencrypt.org/directory"
}

# -- Postgres PITR backup (doc 20; see README §6). All off by default. ------
variable "database_backup_enabled" {
  type        = bool
  description = "Enable Postgres PITR (WAL archiving + scheduled base backups to object storage)."
  default     = false
}

variable "database_backup_destination_path" {
  type        = string
  description = "S3-style backup destination, e.g. s3://bucket/darwin-pg."
  default     = ""
}

variable "database_backup_endpoint_url" {
  type        = string
  description = "Object-store endpoint URL (e.g. Hetzner Object Storage). Empty = AWS S3 default."
  default     = ""
}

variable "database_backup_s3_credentials_secret" {
  type        = string
  description = "Secret (namespace darwin) with ACCESS_KEY_ID + ACCESS_SECRET_KEY for the object store."
  default     = ""
}

variable "database_backup_s3_credentials" {
  type = object({
    access_key_id     = string
    secret_access_key = string
  })
  description = "Object Storage keys for the backup Secret; when set Terraform creates it. Pass via TF_VAR_database_backup_s3_credentials."
  default     = null
  sensitive   = true
}

variable "database_backup_retention_policy" {
  type        = string
  description = "Backup/WAL retention (CNPG syntax, e.g. 30d)."
  default     = "30d"
}

# -- Vault auto-unseal (doc 20; see README §4b). Empty = manual unseal. ------
variable "vault_seal_config_hcl" {
  type        = string
  description = "Raw `seal \"<type>\" { ... }` HCL to enable Vault auto-unseal."
  default     = ""
}

variable "vault_seal_env" {
  type = list(object({
    env_name    = string
    secret_name = string
    secret_key  = string
  }))
  description = "Env the Vault seal stanza needs (e.g. VAULT_TOKEN), each from a Secret key."
  default     = []
}

# -- Kata true microVMs (doc 17; see README §5b). Needs nested virt. ---------
variable "kata_enabled" {
  type        = bool
  description = "Install Kata + register the containerd `kata` handler on nodes. Requires /dev/kvm. Default off → gVisor only."
  default     = false
}

variable "kata_release" {
  type        = string
  description = "Pinned kata-containers release for the kata-static bundle (only used when kata_enabled)."
  default     = "3.21.0"
}

# -- Production supervisor config (see locals.platform_args in main.tf). ----
variable "network" {
  type        = string
  description = "Chain network: testnet (Base Sepolia, staging) or mainnet (Base, prod)."

  validation {
    condition     = contains(["testnet", "mainnet"], var.network)
    error_message = "network must be testnet or mainnet."
  }
}

variable "clerk_issuer" {
  type        = string
  description = "Clerk Frontend API issuer URL (e.g. https://clerk.darwinagents.com)."
}

variable "cors_origins" {
  type        = list(string)
  description = "Dashboard origins allowed by CORS (the exact Vercel domain(s)); never \"*\"."
}

variable "kyc_provider" {
  type        = string
  description = "KYC provider: persona | onfido. Keys come via secret_env."
  default     = "persona"
}

variable "sanctions_provider" {
  type        = string
  description = "Sanctions screening provider: chainalysis | trm. Key via secret_env (DARWIN_SANCTIONS_API_KEY)."
  default     = "chainalysis"
}

variable "fiat_provider" {
  type        = string
  description = "Fiat on/off-ramp: none | moonpay. MoonPay keys via secret_env."
  default     = "none"
}

variable "platform_fee_address" {
  type        = string
  description = "Checksummed 0x address that receives metered charges and the harvest fee."
}

variable "vapid_subject" {
  type        = string
  description = "VAPID subject for web push (mailto: or https: URI). Keys via secret_env."
}

variable "signing_vault_transit_key" {
  type        = string
  description = "Vault transit key that signs audit events and genesis. Create it after Vault init (README)."
  default     = "darwin-platform"
}

variable "store_price_usdc" {
  type        = number
  description = "x402 storefront price per report in USDC. Null = the platform default."
  default     = null
}

variable "x402_facilitator_url" {
  type        = string
  description = "x402 facilitator override. Empty = x402.org on testnet, CDP on mainnet."
  default     = ""
}

variable "cdp_per_tx_usdc" {
  type        = number
  description = "Per-transfer USDC cap enforced at the CDP account (canary safety net). Null = no cap."
  default     = null
}

variable "cdp_per_day_usdc" {
  type        = number
  description = "Per-day USDC cap enforced at the CDP account. Null = no cap."
  default     = null
}

variable "pod_reproduction" {
  type        = bool
  description = "Let Darwins reproduce (the supervisor signs, funds on-chain and spawns each child). Off for the alpha until the audit covers it."
  default     = false
}

variable "database_instances" {
  type        = number
  description = "CNPG Postgres instances (2+ gives a streaming replica for failover)."
  default     = 2
}

variable "secret_env" {
  type        = map(string)
  description = "Supervisor secrets as env (CDP_API_KEY_ID/SECRET, CDP_WALLET_SECRET, DARWIN_GAS_TANK_KEY, DARWIN_SANCTIONS_API_KEY, DARWIN_KYC_PERSONA_*, MOONPAY_*, DARWIN_VAPID_*, E2B_API_KEY, DARWIN_RPC_URL — provider RPC URLs embed a key, …). Composed by the deploy workflow; never commit."
  sensitive   = true
  default     = {}
}
