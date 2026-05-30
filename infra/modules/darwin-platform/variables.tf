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
  description = "Vault root token. Sealed into a Secret the supervisor pod mounts as VAULT_TOKEN. In production this is a Kubernetes-auth-backed AppRole token rather than the root token."
  sensitive   = true
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

variable "service_port" {
  type        = number
  description = "Port the supervisor's API service listens on."
  default     = 8000
}

variable "host_ingress" {
  type        = bool
  description = "If true, install an NGINX Ingress backed by the supervisor Service so the host port mapping (kind extraPortMappings → 80) reaches the API."
  default     = true
}
