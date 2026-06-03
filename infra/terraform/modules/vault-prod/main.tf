/**
 * Vault HA + raft via the official chart. retry_join uses the headless
 * `<release>-internal` Service DNS — the release is named "vault" so the
 * peers are vault-0/1/2.vault-internal:8200.
 *
 * IMPORTANT: wait=false because raft pods report NotReady until they are
 * unsealed. With the default (Shamir) seal an operator unseals manually
 * after every restart; with `seal_config_hcl` set (auto-unseal) a restarted
 * pod unseals itself, but the one-time `vault operator init` still applies —
 * so wait=false / atomic=false stay correct either way. The init/unseal/
 * k8s-auth runbook lives in envs/hetzner/README.md.
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
          # Default: no seal stanza → Shamir → manual unseal runbook.
          # When seal_config_hcl is set, it is injected below to enable
          # auto-unseal (doc 20).
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
            ${var.seal_config_hcl}
          HCL
        }
      }
      # Credentials the seal stanza needs (transit token / cloud-KMS keys),
      # sourced from Secrets so they never land in Terraform state.
      extraSecretEnvironmentVars = [
        for e in var.seal_env : {
          envName    = e.env_name
          secretName = e.secret_name
          secretKey  = e.secret_key
        }
      ]
    }
    # The supervisor uses the Kubernetes auth method + hvac directly, not the
    # agent injector.
    injector = { enabled = false }
    ui       = { enabled = true }
  })]
}
