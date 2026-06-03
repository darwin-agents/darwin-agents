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

locals {
  # PITR backup block, merged into the Cluster spec only when enabled.
  # Setting spec.backup.barmanObjectStore turns on continuous WAL
  # archiving to the object store; the ScheduledBackup CR below adds the
  # periodic base backups. Restore = bootstrap.recovery against the same
  # store with a recoveryTarget (see README runbook).
  backup_spec = var.backup_enabled ? {
    backup = {
      retentionPolicy = var.backup_retention_policy
      barmanObjectStore = merge(
        {
          destinationPath = var.backup_destination_path
          s3Credentials = {
            accessKeyId = {
              name = var.backup_s3_credentials_secret
              key  = "ACCESS_KEY_ID"
            }
            secretAccessKey = {
              name = var.backup_s3_credentials_secret
              key  = "ACCESS_SECRET_KEY"
            }
          }
          wal  = { compression = var.backup_wal_compression }
          data = { compression = var.backup_wal_compression }
        },
        var.backup_endpoint_url != "" ? { endpointURL = var.backup_endpoint_url } : {},
      )
    }
  } : {}
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
    spec = merge({
      instances = var.instances

      bootstrap = {
        initdb = {
          database = var.database_name
          owner    = var.owner_role
        }
      }

      storage = merge(
        { size = var.storage_size },
        var.storage_class != null ? { storageClass = var.storage_class } : {},
      )

      monitoring = {
        # Enable the operator's bundled exporter so kube-prometheus-stack
        # picks the Cluster up automatically once observability lands.
        enablePodMonitor = false
      }
    }, local.backup_spec)
  })

  depends_on = [
    helm_release.operator,
    kubernetes_namespace.data,
  ]
}

# Scheduled base backups (doc 20). WAL is archived continuously by the
# barmanObjectStore above; these periodic base backups bound recovery time
# and let CNPG prune per the retention policy. ``immediate`` takes one at
# apply so PITR is available without waiting for the first cron fire.
resource "kubectl_manifest" "scheduled_backup" {
  count = var.backup_enabled ? 1 : 0

  yaml_body = yamlencode({
    apiVersion = "postgresql.cnpg.io/v1"
    kind       = "ScheduledBackup"
    metadata = {
      name      = "${var.cluster_name}-daily"
      namespace = var.namespace
    }
    spec = {
      schedule              = var.backup_schedule
      immediate             = true
      backupOwnerReference  = "self"
      cluster               = { name = var.cluster_name }
    }
  })

  depends_on = [kubectl_manifest.cluster]
}
