/**
 * Cloud-agnostic Darwin platform composition.
 *
 * Dependency order (Terraform resolves it from the references below):
 *
 *   cnpg     → Postgres operator + Cluster CR (+ connection Secret)
 *   vault    → Vault release
 *   litellm  → LiteLLM proxy
 *   platform → supervisor Deployment/RBAC (wired to the three above)
 *   addons   → ingress controller + the platform Ingress
 *
 * The calling env provides the cluster and configured providers; this
 * module is identical across providers — only the variable values differ.
 */

module "cnpg" {
  source = "../cnpg"

  namespace          = var.namespace
  operator_namespace = var.cnpg_operator_namespace
  cluster_name       = "darwin-platform-db"
  instances          = var.database_instances
  storage_size       = var.database_storage_size
  storage_class      = var.storage_class
}

module "vault" {
  source = "../vault"

  namespace = var.vault_namespace
}

module "litellm" {
  source = "../litellm"

  namespace     = var.litellm_namespace
  master_key    = var.litellm_master_key
  provider_keys = var.litellm_provider_keys
  config_yaml   = var.litellm_config_yaml
}

module "platform" {
  source = "../darwin-platform"

  namespace          = var.namespace
  image              = var.platform_image
  orchestrator_image = var.orchestrator_image
  image_pull_policy  = var.image_pull_policy

  database_secret = module.cnpg.connection_secret_name
  vault_address   = module.vault.address
  vault_token     = module.vault.root_token
  litellm_url     = module.litellm.in_cluster_url
  litellm_key     = var.litellm_master_key

  wallet_provider      = var.wallet_provider
  heartbeat_seconds    = var.heartbeat_seconds
  runtime_class        = var.runtime_class
  create_runtime_class = var.create_runtime_class
  extra_args           = var.extra_args

  depends_on = [module.cnpg, module.vault, module.litellm]
}

module "addons" {
  source = "../k8s-addons"

  namespace    = var.namespace
  service_name = module.platform.service_name
  service_port = var.service_port

  exposure        = var.exposure
  ingress_host    = var.ingress_host
  tls_enabled     = var.tls_enabled
  cluster_issuer  = var.cluster_issuer
  tls_secret_name = var.tls_secret_name

  depends_on = [module.platform]
}
