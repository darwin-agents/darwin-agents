/**
 * Local Darwin platform environment (kind).
 *
 * Thin provider env over the cloud-agnostic `platform-stack` module:
 *
 *   1. module.cluster        — spins up a kind cluster, writes .kubeconfig
 *   2. helm/k8s/kubectl       — providers wired from the cluster's outputs
 *   3. kubectl_manifest.kata  — kind-only RuntimeClass shim (runc-backed)
 *   4. module.platform_stack  — the shared stack (cnpg + vault + litellm +
 *                               supervisor + ingress), with local knobs
 *
 * Adding another provider later = a sibling envs/<provider>/ that swaps
 * module.cluster for that provider's cluster module and passes cloud
 * knobs to the same platform_stack. Nothing in platform-stack changes.
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

/**
 * kind doesn't have kata-containers installed, but the platform's K8s
 * driver stamps runtimeClassName=kata on every per-Darwin pod (the
 * production isolation posture). This shim creates a RuntimeClass named
 * "kata" backed by the default runc handler — the selector resolves
 * locally, no real isolation, fine for the local stack. Cloud envs
 * install a real handler (gVisor `runsc`) and set runtime_class
 * accordingly instead of using this shim.
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

module "platform_stack" {
  source = "../../modules/platform-stack"

  namespace          = var.platform_namespace
  platform_image     = var.platform_image
  orchestrator_image = var.platform_image

  # Local isolation: the runc-backed `kata` shim above, not chart-created.
  runtime_class        = "kata"
  create_runtime_class = false

  # In --driver=k8s the supervisor doesn't host wallets — each per-Darwin
  # orchestrator pod does. `web3` with no chain RPC URLs is the only
  # provider that lets the supervisor pass preflight in k8s mode without
  # external API keys; real chain config lands on each spawned pod.
  wallet_provider = "web3"

  litellm_master_key    = var.litellm_master_key
  litellm_provider_keys = var.litellm_provider_keys
  # Local override: drop the Redis cache section from the docker/ default
  # config, since the local infra stack doesn't deploy Redis.
  litellm_config_yaml = file("${path.module}/litellm.config.yaml")

  # kind exposure: ingress-nginx as a hostNetwork pod → kind host port.
  exposure = "kind-hostport"

  # Allow the local web dashboard (compose, :3000) to call the platform
  # API cross-origin. Without this the supervisor is same-origin only and
  # the browser blocks the dashboard's fetches (CORS preflight 405).
  extra_args = ["--cors-origin", "http://localhost:3000"]

  depends_on = [module.cluster, kubectl_manifest.kata_shim]
}
