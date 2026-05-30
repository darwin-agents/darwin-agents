/**
 * Ingress — fronts the supervisor Service so host port mapping (kind
 * extraPortMappings → 80) lands at the platform API.
 *
 * We install ingress-nginx alongside the platform module rather than as
 * a separate sibling module because it has a dependency-arrow shape
 * (the platform Service is the Ingress's backend; without an Ingress,
 * the host-port mapping has nowhere to land). Keeping them together
 * means `terraform destroy` tears them down in the right order.
 */

resource "kubernetes_namespace" "ingress" {
  count = var.host_ingress ? 1 : 0
  metadata {
    name = "ingress-nginx"
    labels = {
      "app.kubernetes.io/part-of" = "darwin"
    }
  }
}

resource "helm_release" "ingress_nginx" {
  count            = var.host_ingress ? 1 : 0
  name             = "ingress-nginx"
  repository       = "https://kubernetes.github.io/ingress-nginx"
  chart            = "ingress-nginx"
  version          = "4.11.2"
  namespace        = kubernetes_namespace.ingress[0].metadata[0].name
  create_namespace = false
  atomic           = true
  timeout          = 600

  values = [yamlencode({
    controller = {
      # kind's standard pattern — controller runs on the control-plane
      # node (which has the host port mappings) as a hostNetwork pod.
      hostPort = {
        enabled = true
      }
      nodeSelector = {
        "ingress-ready" = "true"
      }
      tolerations = [
        {
          key      = "node-role.kubernetes.io/control-plane"
          operator = "Equal"
          effect   = "NoSchedule"
        }
      ]
      service = {
        type = "ClusterIP"
      }
      publishService = {
        enabled = false
      }
    }
  })]
}

resource "kubernetes_ingress_v1" "platform" {
  count = var.host_ingress ? 1 : 0
  metadata {
    name      = "darwin-platform"
    namespace = var.namespace
    annotations = {
      "nginx.ingress.kubernetes.io/proxy-read-timeout" = "300"
      # The dashboard's WS stream stays open indefinitely; raise the
      # idle timeout so we don't get periodically disconnected.
      "nginx.ingress.kubernetes.io/proxy-send-timeout" = "300"
    }
  }
  spec {
    ingress_class_name = "nginx"
    rule {
      http {
        path {
          path      = "/"
          path_type = "Prefix"
          backend {
            service {
              name = kubernetes_service.platform.metadata[0].name
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
