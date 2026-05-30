/**
 * Local Darwin platform stack.
 *
 * Composition order (top → bottom matches Terraform's dependency
 * resolution; downstream modules wait on their upstream's outputs):
 *
 *   kind-cluster   → produces a kubeconfig at .kubeconfig
 *   helm/k8s/kubectl providers all read that kubeconfig
 *   cnpg           → Postgres operator + a Cluster CR
 *   vault          → Vault Helm release (dev mode)
 *   litellm        → LiteLLM proxy
 *   darwin-platform → supervisor Deployment, RBAC, env wiring
 */

module "cluster" {
  source = "../../modules/kind-cluster"

  cluster_name    = var.cluster_name
  kubeconfig_path = "${path.module}/.kubeconfig"
  # Map host port → ingress-nginx workload port. The platform service
  # gets fronted by a tiny NGINX ingress so the host-facing port is
  # stable across redeploys of the supervisor Deployment.
  host_api_port = var.host_api_port
}

provider "helm" {
  kubernetes {
    config_path = module.cluster.kubeconfig_path
  }
}

provider "kubernetes" {
  config_path = module.cluster.kubeconfig_path
}

provider "kubectl" {
  config_path      = module.cluster.kubeconfig_path
  load_config_file = true
}

module "cnpg" {
  source = "../../modules/cnpg"

  namespace      = var.platform_namespace
  cluster_name   = "darwin-platform-db"
  instances      = 1
  storage_size   = "2Gi"

  depends_on = [module.cluster]
}

module "vault" {
  source = "../../modules/vault"

  namespace = "vault"

  depends_on = [module.cluster]
}

module "litellm" {
  source = "../../modules/litellm"

  namespace      = "litellm"
  master_key     = var.litellm_master_key
  provider_keys  = var.litellm_provider_keys

  depends_on = [module.cluster]
}

module "platform" {
  source = "../../modules/darwin-platform"

  namespace        = var.platform_namespace
  image            = var.platform_image
  database_secret  = module.cnpg.connection_secret_name
  vault_address    = module.vault.address
  vault_token      = module.vault.root_token
  litellm_url      = module.litellm.in_cluster_url
  litellm_key      = var.litellm_master_key

  depends_on = [module.cnpg, module.vault, module.litellm]
}
