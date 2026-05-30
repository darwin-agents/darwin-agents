/**
 * CloudNativePG — operator + a Postgres Cluster.
 *
 * Two phases:
 *   1. Helm releases the operator into its own namespace; this also
 *      installs the CRDs (postgresql.cnpg.io/v1).
 *   2. A kubectl_manifest applies a Cluster custom resource into the
 *      data namespace. CNPG reconciles it into a StatefulSet, PVCs,
 *      and a `<name>-app` Secret holding the connection string the
 *      platform mounts.
 *
 * kubectl_manifest (from alekc/kubectl) is intentionally lazy — it
 * doesn't validate the CRD at plan time, which is exactly what we need
 * because the CRD only exists after the Helm release applies.
 */

resource "kubernetes_namespace" "data" {
  metadata {
    name = var.namespace
    labels = {
      "app.kubernetes.io/part-of" = "darwin"
    }
  }
}

resource "helm_release" "operator" {
  name             = "cnpg"
  repository       = "https://cloudnative-pg.github.io/charts"
  chart            = "cloudnative-pg"
  version          = var.operator_chart_version
  namespace        = var.operator_namespace
  create_namespace = true
  atomic           = true
  timeout          = 600

  # The operator chart owns its CRDs. Default values are fine for local.
}

# Postgres Cluster CR. Connection details land in
# <cluster_name>-app (Secret) which we surface via the output.
resource "kubectl_manifest" "cluster" {
  yaml_body = yamlencode({
    apiVersion = "postgresql.cnpg.io/v1"
    kind       = "Cluster"
    metadata = {
      name      = var.cluster_name
      namespace = var.namespace
    }
    spec = {
      instances = var.instances

      bootstrap = {
        initdb = {
          database = var.database_name
          owner    = var.owner_role
        }
      }

      storage = {
        size = var.storage_size
      }

      monitoring = {
        # Enable the operator's bundled exporter so kube-prometheus-stack
        # picks the Cluster up automatically once observability lands.
        enablePodMonitor = false
      }
    }
  })

  depends_on = [
    helm_release.operator,
    kubernetes_namespace.data,
  ]
}
