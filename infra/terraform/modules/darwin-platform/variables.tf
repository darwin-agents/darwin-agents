variable "namespace" {
  type        = string
  description = "Namespace the supervisor runs in. Must match the CNPG cluster's namespace so the connection-secret mount is in-namespace."
}

variable "image" {
  type        = string
  description = "Image for the supervisor. Build it with `docker build -t darwin-platform:dev .` if running locally."
}

variable "image_pull_policy" {
  type        = string
  description = "imagePullPolicy. IfNotPresent + a `docker build -t darwin-platform:dev` lets kind use the local image; Always pulls from a registry every time."
  default     = "IfNotPresent"
}

variable "replicas" {
  type        = number
  description = "Supervisor replica count. **Always 1 for now** — leader election is in place but persistence-side write paths assume single-writer; bumping replicas needs the leader-election lock wired in first."
  default     = 1
}

variable "leader_election" {
  type        = bool
  description = "Postgres-lock leader election (chart `leaderElection.enabled`). Production requires it even at one replica."
  default     = false
}

variable "database_secret" {
  type        = string
  description = "Name of the Secret (in the same namespace) holding the Postgres connection string. Default CNPG layout exposes the URI under the `uri` key."
}

variable "database_secret_key" {
  type        = string
  description = "Key inside the database secret that holds the connection URI."
  default     = "uri"
}

variable "vault_address" {
  type        = string
  description = "In-cluster Vault address. Used as VAULT_ADDR."
}

variable "vault_token" {
  type        = string
  description = "Vault token for the supervisor (DARWIN_VAULT_TOKEN; e.g. transit signing). Empty in raft/prod mode, where the orchestrator authenticates via the Vault Kubernetes auth method instead of a static token."
  sensitive   = true
  default     = ""
}

variable "litellm_url" {
  type        = string
  description = "In-cluster URL for the LiteLLM proxy. Mounted as DARWIN_LITELLM_URL."
}

variable "litellm_key" {
  type        = string
  description = "LiteLLM master key. Mounted as DARWIN_LITELLM_MASTER_KEY."
  sensitive   = true
}

variable "heartbeat_seconds" {
  type        = number
  description = "Supervisor heartbeat interval. Default 5s matches the `darwin-platform serve` CLI default."
  default     = 5
}

variable "runtime_class" {
  type        = string
  description = "RuntimeClass name the K8s driver stamps on each per-Darwin orchestrator pod (chart `driver.runtimeClassName` → `--k8s-runtime-class`). `kata` (microVM, needs nested virt = bare-metal), `gvisor` (runsc, runs on standard cloud VMs), or `runc`/any installed handler. Local kind installs a runc-backed shim under this name."
  default     = "kata"
}

variable "create_runtime_class" {
  type        = bool
  description = "If true, the chart creates the RuntimeClass resource (handler from runtime_handler). Leave false when the environment installs the RuntimeClass out-of-band (kind does this via a kubectl shim; cloud nodes install the real handler)."
  default     = false
}

variable "runtime_handler" {
  type        = string
  description = "CRI handler the chart-created RuntimeClass points at (only used when create_runtime_class=true). gVisor uses 'runsc'; Kata uses 'kata'."
  default     = "runsc"
}

variable "wallet_provider" {
  type        = string
  description = "Wallet provider for newly-spawned Darwins. Options: `cdp` (Coinbase CDP, requires CDP_API_KEY_*; production default), `local-vault` (per-Darwin keypair sealed in the cluster's Vault; no external dependency for storage but still needs an RPC URL for chain calls), `web3` (legacy shared-key path)."
  default     = "local-vault"
}

variable "extra_args" {
  type        = list(string)
  description = "Additional CLI flags appended to `darwin-platform serve`. Useful for `--auth-tokens` / `--clerk-issuer` / `--push-provider` without forking the module."
  default     = []
}

variable "extra_env" {
  type = list(object({
    name  = string
    value = string
  }))
  description = "Additional environment variables on the supervisor container (chart `extraEnv`). Useful for DARWIN_VAPID_* / provider keys without forking the module."
  default     = []
}

variable "secret_env" {
  type        = map(string)
  description = "Secret environment for the supervisor (e.g. CDP_API_KEY_SECRET, DARWIN_GAS_TANK_KEY, DARWIN_SANCTIONS_API_KEY), stored in its env Secret and mounted via secretKeyRef."
  default     = {}
  sensitive   = true
}

variable "orchestrator_image" {
  type        = string
  description = "Image the K8s driver uses for per-Darwin orchestrator pods (forwarded via --k8s-orchestrator-image). Defaults to the same image as the supervisor so a `docker build -t darwin-platform:dev .` covers both."
  default     = "darwin-platform:dev"
}
