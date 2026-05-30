/**
 * Supervisor Deployment + Service + env wiring.
 *
 * Env contract (matches `darwin-platform serve --help`):
 *   DATABASE_URL              - read by --db (Postgres URI from CNPG)
 *   VAULT_ADDR                - read by Vault SDK
 *   VAULT_TOKEN               - read by Vault SDK
 *   DARWIN_LITELLM_URL        - read by --litellm-url
 *   DARWIN_LITELLM_MASTER_KEY - read by --litellm-master-key
 *
 * CLI args we always pass:
 *   serve --host 0.0.0.0 --port 8000 --db $DATABASE_URL
 *         --driver k8s --heartbeat-seconds N
 *         --litellm-url $DARWIN_LITELLM_URL --litellm-master-key ...
 *
 * Everything else (--auth-tokens, --clerk-issuer, --push-provider, ...)
 * is left to the operator to add by editing the Deployment or extending
 * this module — keeping the default args minimal so an operator sees
 * exactly which knobs they're turning vs which ones are baked in.
 */

resource "kubernetes_secret" "env" {
  metadata {
    name      = "darwin-platform-env"
    namespace = var.namespace
  }
  type = "Opaque"
  data = {
    VAULT_ADDR                = var.vault_address
    VAULT_TOKEN               = var.vault_token
    DARWIN_LITELLM_URL        = var.litellm_url
    DARWIN_LITELLM_MASTER_KEY = var.litellm_key
  }
}

resource "kubernetes_deployment" "platform" {
  metadata {
    name      = "darwin-platform"
    namespace = var.namespace
    labels = {
      "app.kubernetes.io/name"    = "darwin-platform"
      "app.kubernetes.io/part-of" = "darwin"
    }
  }
  spec {
    replicas = var.replicas
    # Recreate strategy so we never run two writers against the same
    # Postgres + Vault namespace (single-writer assumption; see
    # var.replicas).
    strategy {
      type = "Recreate"
    }
    selector {
      match_labels = {
        "app.kubernetes.io/name" = "darwin-platform"
      }
    }
    template {
      metadata {
        labels = {
          "app.kubernetes.io/name"    = "darwin-platform"
          "app.kubernetes.io/part-of" = "darwin"
        }
      }
      spec {
        service_account_name = kubernetes_service_account.platform.metadata[0].name

        container {
          name              = "platform"
          image             = var.image
          image_pull_policy = var.image_pull_policy

          command = ["darwin-platform"]
          args = [
            "serve",
            "--host", "0.0.0.0",
            "--port", tostring(var.service_port),
            "--driver", "k8s",
            "--heartbeat-seconds", tostring(var.heartbeat_seconds),
            "--db", "$(DATABASE_URL)",
            "--litellm-url", "$(DARWIN_LITELLM_URL)",
            "--litellm-master-key", "$(DARWIN_LITELLM_MASTER_KEY)",
          ]

          # Argv substitutions reference these envFrom-injected keys.
          env_from {
            secret_ref {
              name = kubernetes_secret.env.metadata[0].name
            }
          }

          # CNPG writes the connection URI under spec.database_secret_key.
          env {
            name = "DATABASE_URL"
            value_from {
              secret_key_ref {
                name = var.database_secret
                key  = var.database_secret_key
              }
            }
          }

          port {
            container_port = var.service_port
            name           = "http"
            protocol       = "TCP"
          }

          readiness_probe {
            http_get {
              path = "/readyz"
              port = var.service_port
            }
            initial_delay_seconds = 5
            period_seconds        = 5
            failure_threshold     = 6
          }
          liveness_probe {
            http_get {
              path = "/healthz"
              port = var.service_port
            }
            initial_delay_seconds = 15
            period_seconds        = 20
          }

          resources {
            requests = {
              cpu    = "200m"
              memory = "512Mi"
            }
            limits = {
              cpu    = "2"
              memory = "2Gi"
            }
          }
        }
      }
    }
  }

  depends_on = [
    kubernetes_cluster_role_binding.platform,
  ]
}

resource "kubernetes_service" "platform" {
  metadata {
    name      = "darwin-platform"
    namespace = var.namespace
  }
  spec {
    type = "ClusterIP"
    selector = {
      "app.kubernetes.io/name" = "darwin-platform"
    }
    port {
      name        = "http"
      port        = var.service_port
      target_port = var.service_port
      protocol    = "TCP"
    }
  }
}
