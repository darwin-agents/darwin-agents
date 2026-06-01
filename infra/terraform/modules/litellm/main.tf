/**
 * LiteLLM proxy — Deployment + Service + ConfigMap + Secret.
 *
 * No upstream BerriAI Helm chart at the time of writing (their compose
 * stack is the documented path). Plain kubernetes_* resources keep the
 * blast radius tight and let the platform module's deps wait on the
 * config_map / secret rather than a Helm release status.
 *
 * Config strategy:
 *   - litellm.config.yaml lives in a ConfigMap, mounted at /app/config.yaml
 *   - LLM provider keys live in a Secret, surfaced as env vars
 *   - The proxy reads `os.environ/<KEY>` placeholders from the config
 *     and resolves them at request time
 */

locals {
  # Default to the repo's docker config. The path is relative to this
  # module dir; ../../docker/... walks up to repo root via env/local/.
  default_config_path = "${path.module}/../../../docker/litellm.config.yaml"
  config_yaml = (
    var.config_yaml != null
    ? var.config_yaml
    : file(local.default_config_path)
  )
}

resource "kubernetes_namespace" "this" {
  metadata {
    name = var.namespace
    labels = {
      "app.kubernetes.io/part-of" = "darwin"
    }
  }
}

resource "kubernetes_config_map" "config" {
  metadata {
    name      = "litellm-config"
    namespace = kubernetes_namespace.this.metadata[0].name
  }
  data = {
    "config.yaml" = local.config_yaml
  }
}

resource "kubernetes_secret" "env" {
  metadata {
    name      = "litellm-env"
    namespace = kubernetes_namespace.this.metadata[0].name
  }
  type = "Opaque"
  data = merge(
    {
      DARWIN_LITELLM_MASTER_KEY = var.master_key
    },
    var.provider_keys,
  )
}

resource "kubernetes_deployment" "this" {
  metadata {
    name      = "litellm"
    namespace = kubernetes_namespace.this.metadata[0].name
    labels = {
      "app.kubernetes.io/name"    = "litellm"
      "app.kubernetes.io/part-of" = "darwin"
    }
  }
  spec {
    replicas = var.replicas
    selector {
      match_labels = {
        "app.kubernetes.io/name" = "litellm"
      }
    }
    template {
      metadata {
        labels = {
          "app.kubernetes.io/name"    = "litellm"
          "app.kubernetes.io/part-of" = "darwin"
        }
      }
      spec {
        container {
          name  = "proxy"
          image = var.image
          # BerriAI's image launches the proxy when --config is set.
          # NOTE: don't mount onto /app — that's the image's WORKDIR and
          # contains the `docker/prod_entrypoint.sh` the image's
          # ENTRYPOINT references. We use a subPath into a non-app dir.
          args = ["--config", "/etc/litellm/config.yaml", "--port", "4000"]

          port {
            container_port = 4000
            name           = "http"
          }

          env_from {
            secret_ref {
              name = kubernetes_secret.env.metadata[0].name
            }
          }

          volume_mount {
            name       = "config"
            mount_path = "/etc/litellm/config.yaml"
            sub_path   = "config.yaml"
            read_only  = true
          }

          # Readiness — the proxy answers /health/readiness on :4000.
          readiness_probe {
            http_get {
              path = "/health/readiness"
              port = 4000
            }
            initial_delay_seconds = 5
            period_seconds        = 5
          }
          liveness_probe {
            http_get {
              path = "/health/liveness"
              port = 4000
            }
            initial_delay_seconds = 15
            period_seconds        = 20
          }

          resources {
            requests = {
              cpu    = "100m"
              memory = "512Mi"
            }
            limits = {
              cpu = "1"
              # 1Gi was getting OOMKilled at boot — the proxy pulls in a
              # big SDK surface (every provider's SDK) before listening,
              # which spikes RSS well over 1Gi briefly.
              memory = "2Gi"
            }
          }
        }

        volume {
          name = "config"
          config_map {
            name = kubernetes_config_map.config.metadata[0].name
          }
        }
      }
    }
  }
}

resource "kubernetes_service" "this" {
  metadata {
    name      = "litellm"
    namespace = kubernetes_namespace.this.metadata[0].name
  }
  spec {
    type = "ClusterIP"
    selector = {
      "app.kubernetes.io/name" = "litellm"
    }
    port {
      name        = "http"
      port        = 4000
      target_port = 4000
      protocol    = "TCP"
    }
  }
}
