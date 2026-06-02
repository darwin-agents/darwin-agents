/**
 * Ingress controller + the platform Ingress resource.
 *
 * Factored out of modules/darwin-platform/ingress.tf so the exposure
 * strategy is a per-environment knob (var.exposure) rather than baked
 * into the supervisor module. The controller and the Ingress object are
 * kept together so `terraform destroy` tears them down in the right
 * order (the Ingress's backend is the platform Service; the controller
 * is what makes the host port / LoadBalancer land).
 */

locals {
  enabled = var.exposure != "none"

  # Per-exposure ingress-nginx controller values.
  controller_values = {
    # kind: the controller runs as a hostNetwork pod pinned to the
    # control-plane node, which carries kind's extraPortMappings → 80.
    "kind-hostport" = {
      controller = {
        hostPort     = { enabled = true }
        nodeSelector = { "ingress-ready" = "true" }
        tolerations = [{
          key      = "node-role.kubernetes.io/control-plane"
          operator = "Equal"
          effect   = "NoSchedule"
        }]
        service        = { type = "ClusterIP" }
        publishService = { enabled = false }
      }
    }
    # cloud: the controller sits behind a managed LoadBalancer. Needs a
    # cloud controller manager to provision the external IP (Phase 3
    # wires hcloud-cloud-controller-manager).
    "loadbalancer" = {
      controller = {
        service        = { type = "LoadBalancer" }
        publishService = { enabled = true }
      }
    }
  }

  # cert-manager issues the cert when a ClusterIssuer is named.
  cert_manager_annotations = (var.tls_enabled && var.cluster_issuer != "") ? {
    "cert-manager.io/cluster-issuer" = var.cluster_issuer
  } : {}

  ingress_annotations = merge(
    {
      "nginx.ingress.kubernetes.io/proxy-read-timeout" = tostring(var.proxy_read_timeout)
      "nginx.ingress.kubernetes.io/proxy-send-timeout" = tostring(var.proxy_send_timeout)
    },
    local.cert_manager_annotations,
  )
}

resource "kubernetes_namespace" "ingress" {
  count = local.enabled ? 1 : 0
  metadata {
    name = "ingress-nginx"
    labels = {
      "app.kubernetes.io/part-of" = "darwin"
    }
  }
}

resource "helm_release" "ingress_nginx" {
  count            = local.enabled ? 1 : 0
  name             = "ingress-nginx"
  repository       = "https://kubernetes.github.io/ingress-nginx"
  chart            = "ingress-nginx"
  version          = var.ingress_nginx_chart_version
  namespace        = kubernetes_namespace.ingress[0].metadata[0].name
  create_namespace = false
  atomic           = true
  timeout          = 600

  values = [yamlencode(local.controller_values[var.exposure])]
}

resource "kubernetes_ingress_v1" "platform" {
  count = local.enabled ? 1 : 0
  metadata {
    name        = "darwin-platform"
    namespace   = var.namespace
    annotations = local.ingress_annotations
  }
  spec {
    ingress_class_name = var.ingress_class_name

    # TLS termination at the Ingress. Either cert-manager fills
    # tls_secret_name (via cluster_issuer) or it's pre-provisioned.
    dynamic "tls" {
      for_each = (var.tls_enabled && var.ingress_host != "") ? [1] : []
      content {
        hosts       = [var.ingress_host]
        secret_name = var.tls_secret_name
      }
    }

    rule {
      # Empty host = match all (kind/local). A real domain scopes the rule.
      host = var.ingress_host != "" ? var.ingress_host : null
      http {
        path {
          path      = "/"
          path_type = "Prefix"
          backend {
            service {
              name = var.service_name
              port {
                number = var.service_port
              }
            }
          }
        }
      }
    }
  }

  depends_on = [helm_release.ingress_nginx]
}
