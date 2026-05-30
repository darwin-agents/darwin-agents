/**
 * kind cluster bootstrap.
 *
 * Three nodes: one control-plane + two workers. Two workers is the
 * minimum that lets us schedule the platform supervisor on one and
 * have headroom for per-Darwin pods on the others without binpacking
 * everything onto the same node (which would make a single bad pod
 * starve the whole cluster).
 *
 * The control-plane node carries an ingress-nginx-ready label so the
 * ingress-nginx Helm chart (installed by the platform module) lands
 * on a node whose ports are mapped to the host.
 */
resource "kind_cluster" "this" {
  name           = var.cluster_name
  node_image     = var.node_image
  kubeconfig_path = var.kubeconfig_path
  wait_for_ready  = true

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    # Control plane — ingress lives here so the kind port mappings are
    # stable. The 'ingress-ready' label is the standard ingress-nginx
    # nodeSelector value.
    node {
      role = "control-plane"

      kubeadm_config_patches = [
        "kind: InitConfiguration\nnodeRegistration:\n  kubeletExtraArgs:\n    node-labels: \"ingress-ready=true\"\n",
      ]

      extra_port_mappings {
        container_port = 80
        host_port      = var.host_api_port
        protocol       = "TCP"
        listen_address = "0.0.0.0"
      }

      extra_port_mappings {
        container_port = 443
        host_port      = var.host_extra_port
        protocol       = "TCP"
        listen_address = "0.0.0.0"
      }
    }

    node {
      role = "worker"
    }

    node {
      role = "worker"
    }
  }
}
