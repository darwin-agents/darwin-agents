/**
 * Hetzner in-cluster addons. The `hcloud` secret in kube-system is shared by
 * the CCM (token + network) and the CSI driver (token). Helm releases use
 * wait=true so the platform stack (which depends on this module) only starts
 * after the node taint is cleared and the StorageClass exists.
 */

resource "kubernetes_secret" "hcloud" {
  metadata {
    name      = "hcloud"
    namespace = "kube-system"
  }
  type = "Opaque"
  data = {
    token   = var.hcloud_token
    network = var.network_name
  }
}

resource "helm_release" "ccm" {
  name       = "hccm"
  repository = "https://charts.hetzner.cloud"
  chart      = "hcloud-cloud-controller-manager"
  version    = var.ccm_chart_version
  namespace  = "kube-system"
  atomic     = true
  wait       = true
  timeout    = 600

  values = [yamlencode({
    networking = {
      enabled     = true
      clusterCIDR = var.cluster_cidr # MUST match k3s --cluster-cidr
    }
    # Cluster-wide LoadBalancer defaults (a Service can still override via
    # annotations). The chart already wires HCLOUD_TOKEN/HCLOUD_NETWORK from
    # the `hcloud` secret — only set the LB knobs here.
    env = {
      HCLOUD_LOAD_BALANCERS_LOCATION       = { value = var.lb_location }
      HCLOUD_LOAD_BALANCERS_USE_PRIVATE_IP = { value = "true" }
    }
  })]

  depends_on = [kubernetes_secret.hcloud]
}

resource "helm_release" "csi" {
  name       = "hcloud-csi"
  repository = "https://charts.hetzner.cloud"
  chart      = "hcloud-csi"
  version    = var.csi_chart_version
  namespace  = "kube-system"
  atomic     = true
  wait       = true
  timeout    = 600

  # Default values create the `hcloud-volumes` StorageClass (ext4, Delete,
  # WaitForFirstConsumer, allowVolumeExpansion). Reads the `hcloud` secret.
  depends_on = [kubernetes_secret.hcloud]
}
