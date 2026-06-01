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

/**
 * Provider configs use inline credentials sourced from the kind
 * module's `data.external` (which runs `kubectl config view` against
 * the kubeconfig kind wrote, AFTER the cluster is up). This sidesteps
 * the eager-config-validation problem: passing `config_path` to the
 * kubernetes provider fails at plan time if the file doesn't exist yet,
 * which it doesn't on a fresh `terraform apply`. Inline values are
 * unknown at plan time and lazily resolved at apply, which is fine.
 */
provider "helm" {
  kubernetes {
    host                   = module.cluster.endpoint
    cluster_ca_certificate = module.cluster.cluster_ca_certificate
    client_certificate     = module.cluster.client_certificate
    client_key             = module.cluster.client_key
  }
}

provider "kubernetes" {
  host                   = module.cluster.endpoint
  cluster_ca_certificate = module.cluster.cluster_ca_certificate
  client_certificate     = module.cluster.client_certificate
  client_key             = module.cluster.client_key
}

provider "kubectl" {
  host                   = module.cluster.endpoint
  cluster_ca_certificate = module.cluster.cluster_ca_certificate
  client_certificate     = module.cluster.client_certificate
  client_key             = module.cluster.client_key
  load_config_file       = false
  lazy_load              = true
}

module "cnpg" {
  source = "../../modules/cnpg"

  namespace    = var.platform_namespace
  cluster_name = "darwin-platform-db"
  instances    = 1
  storage_size = "2Gi"

  depends_on = [module.cluster]
}

module "vault" {
  source = "../../modules/vault"

  namespace = "vault"

  depends_on = [module.cluster]
}

/**
 * kind doesn't have kata-containers installed, but the platform's K8s
 * driver hard-defaults runtimeClassName=kata on every per-Darwin pod
 * (production isolation posture). This shim creates a RuntimeClass
 * named "kata" backed by the default runc handler — the selector
 * resolves locally, no real isolation, fine for the local stack.
 * Production swaps this for a real kata install.
 */
resource "kubectl_manifest" "kata_shim" {
  yaml_body = yamlencode({
    apiVersion = "node.k8s.io/v1"
    kind       = "RuntimeClass"
    metadata = {
      name = "kata"
      labels = {
        "app.kubernetes.io/part-of" = "darwin"
      }
    }
    handler = "runc"
  })

  depends_on = [module.cluster]
}

module "litellm" {
  source = "../../modules/litellm"

  namespace     = "litellm"
  master_key    = var.litellm_master_key
  provider_keys = var.litellm_provider_keys
  # Local override: drop the Redis cache section from the docker/
  # default config, since the local infra stack doesn't deploy Redis.
  config_yaml = file("${path.module}/litellm.config.yaml")

  depends_on = [module.cluster]
}

module "platform" {
  source = "../../modules/darwin-platform"

  namespace       = var.platform_namespace
  image           = var.platform_image
  database_secret = module.cnpg.connection_secret_name
  vault_address   = module.vault.address
  vault_token     = module.vault.root_token
  litellm_url     = module.litellm.in_cluster_url
  litellm_key     = var.litellm_master_key
  # In --driver=k8s the supervisor doesn't host wallets — each
  # per-Darwin orchestrator pod does. We still set --wallet-provider
  # because it's forwarded into DarwinPodConfig for the spawned pods.
  # `web3` with no chain RPC URLs is the only provider that lets the
  # supervisor itself pass preflight in k8s mode without external API
  # keys; the real chain config lands on each spawned pod separately.
  wallet_provider = "web3"

  # Allow the local web dashboard (compose, :3000) to call the platform
  # API cross-origin. Without this the supervisor is same-origin only and
  # the browser blocks the dashboard's fetches (CORS preflight 405).
  extra_args = ["--cors-origin", "http://localhost:3000"]

  depends_on = [module.cnpg, module.vault, module.litellm]
}
