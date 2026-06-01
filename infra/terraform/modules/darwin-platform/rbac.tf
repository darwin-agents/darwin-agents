/**
 * RBAC for the K8s driver (src/darwin_platform/k8s/driver.py).
 *
 * The driver creates one namespace per Darwin and, within each, the
 * full set of resources its manifest builder emits: ConfigMap, Secret,
 * PVC, Service, Deployment, NetworkPolicy. Tearing down a Darwin
 * deletes the whole namespace.
 *
 * We grant ClusterRole-scoped permissions because per-Darwin namespaces
 * don't exist at plan time. A namespace-scoped Role would have to be
 * re-applied for every new Darwin — exactly the bookkeeping the
 * cluster-scoped role avoids.
 *
 * NOT granted: anything cluster-scoped that doesn't fit the
 * per-Darwin-namespace model (nodes, RBAC, PV, CRDs). The driver
 * doesn't need them and shouldn't have them.
 */

resource "kubernetes_service_account" "platform" {
  metadata {
    name      = "darwin-platform"
    namespace = var.namespace
    labels = {
      "app.kubernetes.io/part-of" = "darwin"
    }
  }
}

resource "kubernetes_cluster_role" "platform" {
  metadata {
    name = "darwin-platform"
    labels = {
      "app.kubernetes.io/part-of" = "darwin"
    }
  }

  rule {
    api_groups = [""]
    resources  = ["namespaces"]
    verbs      = ["create", "delete", "get", "list", "watch"]
  }

  rule {
    api_groups = [""]
    resources  = ["configmaps", "secrets", "services", "persistentvolumeclaims"]
    verbs      = ["create", "delete", "get", "list", "patch", "update", "watch"]
  }

  rule {
    api_groups = ["apps"]
    resources  = ["deployments", "deployments/status"]
    verbs      = ["create", "delete", "get", "list", "patch", "update", "watch"]
  }

  rule {
    api_groups = ["networking.k8s.io"]
    resources  = ["networkpolicies"]
    verbs      = ["create", "delete", "get", "list", "patch", "update", "watch"]
  }

  # Read-only on pods so the driver can poll readiness without write
  # access to the pod resource directly (write happens via Deployment).
  rule {
    api_groups = [""]
    resources  = ["pods", "pods/log", "events"]
    verbs      = ["get", "list", "watch"]
  }
}

resource "kubernetes_cluster_role_binding" "platform" {
  metadata {
    name = "darwin-platform"
    labels = {
      "app.kubernetes.io/part-of" = "darwin"
    }
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role.platform.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account.platform.metadata[0].name
    namespace = var.namespace
  }
}
