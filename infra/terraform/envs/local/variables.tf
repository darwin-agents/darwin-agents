variable "cluster_name" {
  type        = string
  description = "kind cluster name + the prefix used to namespace anything tagged outside Kubernetes."
  default     = "darwin"
}

variable "platform_image" {
  type        = string
  description = "Container image to run for the darwin-platform supervisor. Built locally with `docker build -t darwin-platform:dev .` if you want to use a non-published image."
  default     = "darwin-platform:dev"
}

variable "platform_namespace" {
  type        = string
  description = "Kubernetes namespace that the platform supervisor itself runs in. Per-Darwin namespaces (`darwin-<id>`) are created at runtime by the K8s driver."
  default     = "darwin"
}

variable "host_api_port" {
  type        = number
  description = "Port on the host (your laptop) that the platform API is reachable at. Mapped to the cluster's ingress/service via kind extraPortMappings."
  default     = 8000
}

variable "litellm_master_key" {
  type        = string
  description = "Master key for the LiteLLM proxy admin API. Stays inside the cluster; the platform mounts it as DARWIN_LITELLM_MASTER_KEY."
  sensitive   = true
  default     = "sk-local-litellm-dev"
}

variable "litellm_provider_keys" {
  type        = map(string)
  description = "Map of upstream LLM provider API keys (OPENAI_API_KEY, ANTHROPIC_API_KEY, ...) that LiteLLM proxies on behalf of the platform. Leave empty to skip — LiteLLM still installs and responds, but every model call returns 401 until you populate this."
  sensitive   = true
  default     = {}
}
