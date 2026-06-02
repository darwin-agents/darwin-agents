variable "namespace" {
  type        = string
  description = "Namespace the LiteLLM proxy runs in."
  default     = "litellm"
}

variable "image" {
  type        = string
  description = "LiteLLM container image. Use the published BerriAI image unless you have a fork."
  default     = "ghcr.io/berriai/litellm:main-stable"
}

variable "replicas" {
  type        = number
  description = "Proxy replica count. 1 for local; ≥2 for production (cross-replica rate limits coordinate via Redis, which is a future addition)."
  default     = 1
}

variable "master_key" {
  type        = string
  description = "LiteLLM master key. The platform mounts this as --litellm-master-key."
  sensitive   = true
}

variable "provider_keys" {
  type        = map(string)
  description = "Upstream LLM provider API keys. Keys here become env vars on the proxy pod (OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, CLAUDE_API_KEY, ...). Leave empty for a 'proxy is up but every call 401s' install."
  sensitive   = true
  default     = {}
}

variable "config_yaml" {
  type        = string
  description = "Optional override for the LiteLLM proxy config. Default is the model_list from infra/docker/litellm.config.yaml (read at apply time)."
  default     = null
}

variable "database_url" {
  type        = string
  description = "Postgres connection URL backing LiteLLM's virtual-key + per-key spend tables (doc 13). REQUIRED for per-Darwin virtual keys: without a database the proxy cannot persist keys generated via /key/generate, so the platform's --litellm-virtual-keys mode has nowhere to mint them and no spend table to reconcile against. Empty = no DB (dev only; shared-master-key mode). The platform already runs CloudNativePG; create a `litellm` database there and pass its connection URL."
  sensitive   = true
  default     = ""
}
