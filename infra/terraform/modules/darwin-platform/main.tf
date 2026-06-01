/**
 * Supervisor deployment via the canonical Helm chart.
 *
 * This module is a thin Terraform wrapper around
 * `infra/charts/darwin-platform` — that chart is the single source of
 * truth for the supervisor's Kubernetes workload (Deployment, Service,
 * ServiceAccount, and the K8s-driver ClusterRole + Binding). The module's
 * job is to (a) wire the chart's values to the sibling modules' outputs
 * (CNPG Postgres, Vault, LiteLLM) and (b) carry the local-stack glue the
 * chart deliberately doesn't model: the DARWIN_POD_* forwarding Secret
 * (substrate creds the K8s driver mounts onto spawned pods) and the NGINX
 * Ingress that fronts the API on the kind host port (see ingress.tf).
 *
 * Posture: the local stack runs the chart's k8s driver mode but strips
 * the production extras the chart enables by default — no egress proxy,
 * no per-Darwin NetworkPolicies, no LanceDB sidecar (no public image),
 * no real Kata isolation (envs/local installs a runc-backed `kata`
 * RuntimeClass shim). Those map to the `*.enabled = false` /
 * `lancedb.image = ""` values below. Everything the chart's k8s args
 * leave implicit (`--k8s-namespace-prefix`, `--k8s-runtime-class`,
 * `--k8s-egress-proxy-namespace`) resolves to the supervisor's CLI
 * defaults, which match what this module used to pass by hand.
 */

locals {
  # Release name == chart fullname == Service / ServiceAccount name
  # (the fullname helper collapses to Release.Name when it already
  # contains the chart name, which "darwin-platform" does).
  release_name = "darwin-platform"

  # Split "repo:tag" refs into the chart's repository/tag fields. Local
  # images are unqualified ("darwin-platform:dev"), so a ":" split is
  # sufficient; registry-qualified refs with a port would need more care.
  image_parts             = split(":", var.image)
  image_repository        = local.image_parts[0]
  image_tag               = length(local.image_parts) > 1 ? local.image_parts[1] : "latest"
  orchestrator_parts      = split(":", var.orchestrator_image)
  orchestrator_repository = local.orchestrator_parts[0]
  orchestrator_tag        = length(local.orchestrator_parts) > 1 ? local.orchestrator_parts[1] : "latest"

  # Substrate creds the K8s driver forwards into each per-Darwin
  # orchestrator pod via DarwinPodConfig. Projected onto the supervisor
  # as env (chart `extraEnv`), where the driver reads them at spawn time.
  pod_env_keys = [
    "DARWIN_POD_VAULT_ADDR",
    "DARWIN_POD_VAULT_TOKEN",
    "DARWIN_POD_LITELLM_URL",
    "DARWIN_POD_LITELLM_KEY",
  ]
}

# Kept in Terraform (not the chart) because the values come from sibling
# modules (Vault, LiteLLM) and are local-stack wiring, not part of the
# supervisor's own chart surface. In --driver=k8s the supervisor doesn't
# talk to LiteLLM/Vault directly — setting DARWIN_LITELLM_URL / VAULT_ADDR
# would trip the "real-adapter flags not supported with --driver=k8s"
# preflight — so these are stored under DARWIN_POD_* keys the driver
# mounts onto spawned pods instead.
resource "kubernetes_secret" "env" {
  metadata {
    name      = "darwin-platform-env"
    namespace = var.namespace
  }
  type = "Opaque"
  data = {
    DARWIN_POD_VAULT_ADDR  = var.vault_address
    DARWIN_POD_VAULT_TOKEN = var.vault_token
    DARWIN_POD_LITELLM_URL = var.litellm_url
    DARWIN_POD_LITELLM_KEY = var.litellm_key
  }
}

resource "helm_release" "platform" {
  name             = local.release_name
  chart            = "${path.module}/../../../charts/darwin-platform"
  namespace        = var.namespace
  create_namespace = false # CNPG creates the namespace; we run alongside it.
  atomic           = true
  wait             = true
  timeout          = 600

  values = [yamlencode({
    image = {
      repository = local.image_repository
      tag        = local.image_tag
      pullPolicy = var.image_pull_policy
    }
    replicaCount = var.replicas

    # Postgres-backed persistence: the CNPG connection Secret carries the
    # URI under var.database_secret_key. Setting existingSecret switches
    # the chart to `--db=$(DATABASE_URL)` and skips the SQLite PVC.
    persistence = {
      enabled = false
    }
    database = {
      existingSecret    = var.database_secret
      existingSecretKey = var.database_secret_key
    }

    # Preserve this module's original resource envelope (the chart's
    # defaults are smaller).
    resources = {
      requests = { cpu = "200m", memory = "512Mi" }
      limits   = { cpu = "2", memory = "2Gi" }
    }

    # Supervisor runtime flags (chart 0.3.0+ passthroughs).
    walletProvider   = var.wallet_provider
    heartbeatSeconds = var.heartbeat_seconds
    extraArgs        = var.extra_args

    # K8s driver mode, stripped to the local posture.
    driver = {
      mode = "k8s"
      orchestrator = {
        image = {
          repository = local.orchestrator_repository
          tag        = local.orchestrator_tag
        }
      }
      # No public lancedb image → skip the sidecar (the orchestrator
      # falls back to the deterministic in-memory EpisodicMemory).
      lancedb = {
        image = ""
      }
    }

    # Production extras the local stack doesn't run. The supervisor still
    # tells spawned pods about the (default) egress-proxy namespace, but
    # nothing here creates a proxy or per-Darwin NetworkPolicies.
    networkPolicy         = { enabled = false }
    egressProxy           = { enabled = false }
    platformDataNamespace = { create = false }
    runtimeClass          = { create = false } # envs/local installs the kata shim
    serviceMonitor        = { enabled = false }
    ingress               = { enabled = false } # fronted by ingress.tf instead

    # DARWIN_POD_* substrate creds, sourced from the Secret above.
    extraEnv = [
      for k in local.pod_env_keys : {
        name = k
        valueFrom = {
          secretKeyRef = {
            name = kubernetes_secret.env.metadata[0].name
            key  = k
          }
        }
      }
    ]
  })]

  # The Secret must exist before the pod references it via extraEnv.
  depends_on = [kubernetes_secret.env]
}
