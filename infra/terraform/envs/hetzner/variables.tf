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
  default     = "ghcr.io/quantium-rock/darwin-agent/darwin-platform:latest"
}

variable "wallet_provider" {
  type        = string
  description = "Wallet provider for spawned darwins: cdp | local-vault | web3. Production default cdp (add CDP keys to the per-darwin substrate secret)."
  default     = "cdp"
}

variable "allowed_admin_cidrs" {
  type        = list(string)
  description = "Source CIDRs allowed to reach SSH (22) + kube API (6443). LOCK THIS DOWN to your IP."
  default     = ["0.0.0.0/0"]
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
  description = "LiteLLM proxy admin key."
  sensitive   = true
  default     = "sk-hetzner-litellm-change-me"
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
