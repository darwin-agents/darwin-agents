/**
 * Vault HA + raft via the official chart. retry_join uses the headless
 * `<release>-internal` Service DNS — the release is named "vault" so the
 * peers are vault-0/1/2.vault-internal:8200.
 *
 * IMPORTANT: wait=false because raft pods report NotReady until an operator
 * unseals them (manual, no KMS). atomic=false so a "not ready in time"
 * doesn't roll the release back. The unseal/init/k8s-auth runbook lives in
 * envs/hetzner/README.md.
 */

resource "kubernetes_namespace" "this" {
  metadata {
    name = var.namespace
    labels = {
      "app.kubernetes.io/part-of" = "darwin"
    }
  }
}

resource "helm_release" "vault" {
  name       = "vault"
  repository = "https://helm.releases.hashicorp.com"
  chart      = "vault"
  version    = var.chart_version
  namespace  = kubernetes_namespace.this.metadata[0].name

  # Raft pods stay NotReady until manually unsealed — never block on readiness.
  wait    = false
  atomic  = false
  timeout = 600

  values = [yamlencode({
    global = {
      enabled    = true
      tlsDisable = true # terminate TLS at the ingress/mesh; raft listener is in-cluster
    }
    server = {
      image    = { tag = var.vault_image_tag }
      logLevel = "info"
      dataStorage = {
        enabled      = true
        size         = var.data_storage_size
        mountPath    = "/vault/data"
        storageClass = var.storage_class
        accessMode   = "ReadWriteOnce"
      }
      auditStorage = {
        enabled      = true
        size         = var.audit_storage_size
        storageClass = var.storage_class
        accessMode   = "ReadWriteOnce"
      }
      ha = {
        enabled  = true
        replicas = var.replicas
        raft = {
          enabled   = true
          setNodeId = true
          # NO seal {} stanza → no auto-unseal → manual unseal runbook.
          config = <<-HCL
            ui = true
            listener "tcp" {
              address         = "[::]:8200"
              cluster_address = "[::]:8201"
              tls_disable     = 1
            }
            storage "raft" {
              path = "/vault/data"
              retry_join { leader_api_addr = "http://vault-0.vault-internal:8200" }
              retry_join { leader_api_addr = "http://vault-1.vault-internal:8200" }
              retry_join { leader_api_addr = "http://vault-2.vault-internal:8200" }
            }
            service_registration "kubernetes" {}
          HCL
        }
      }
    }
    # The supervisor uses the Kubernetes auth method + hvac directly, not the
    # agent injector.
    injector = { enabled = false }
    ui       = { enabled = true }
  })]
}
