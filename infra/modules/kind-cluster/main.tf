/**
 * kind cluster bootstrap via the `kind` CLI.
 *
 * We use a `null_resource` + local-exec instead of the tehcyx/kind
 * provider because the provider's "wait for systemd ready" log-line
 * regex doesn't match on every host (cgroup v2 + recent systemd
 * versions print a different startup line, leading to spurious
 * "could not find a log line that matches" errors on `terraform
 * apply`). The CLI form just shells out to `kind` and is robust to
 * those variations — `kind` itself knows when its cluster is ready.
 *
 * Three nodes: one control-plane + two workers. Two workers is the
 * minimum that lets us schedule the platform supervisor on one and
 * have headroom for per-Darwin pods on the others without binpacking
 * everything onto the same node (which would make a single bad pod
 * starve the whole cluster).
 *
 * The control-plane node carries an ingress-ready label so the
 * ingress-nginx Helm chart (installed by the platform module) lands
 * on a node whose ports are mapped to the host.
 */

locals {
  kind_config_yaml = yamlencode({
    kind       = "Cluster"
    apiVersion = "kind.x-k8s.io/v1alpha4"
    nodes = [
      {
        role = "control-plane"
        kubeadmConfigPatches = [
          # ingress-nginx's standard nodeSelector value.
          "kind: InitConfiguration\nnodeRegistration:\n  kubeletExtraArgs:\n    node-labels: \"ingress-ready=true\"\n",
        ]
        extraPortMappings = [
          {
            containerPort = 80
            hostPort      = var.host_api_port
            protocol      = "TCP"
            listenAddress = "0.0.0.0"
          },
          {
            containerPort = 443
            hostPort      = var.host_extra_port
            protocol      = "TCP"
            listenAddress = "0.0.0.0"
          },
        ]
      },
      { role = "worker" },
      { role = "worker" },
    ]
  })
}

# Persist the rendered config so a destroy can read it (and a reapply
# can diff it). Using local_file rather than passing via stdin keeps
# the cluster reproducible from disk if you ever need to recreate by
# hand outside Terraform.
resource "local_file" "kind_config" {
  filename        = "${path.module}/.kind-config-${var.cluster_name}.yaml"
  content         = local.kind_config_yaml
  file_permission = "0644"
}

resource "null_resource" "cluster" {
  triggers = {
    # Recreate if any of these change. We don't trigger on every
    # variable — just the ones that affect the on-disk cluster shape.
    cluster_name    = var.cluster_name
    node_image      = var.node_image
    config_hash     = sha256(local.kind_config_yaml)
    kubeconfig_path = var.kubeconfig_path
  }

  provisioner "local-exec" {
    when    = create
    command = "kind create cluster --name ${self.triggers.cluster_name} --image ${self.triggers.node_image} --config ${local_file.kind_config.filename} --kubeconfig ${self.triggers.kubeconfig_path} --wait 120s"
  }

  provisioner "local-exec" {
    when    = destroy
    command = "kind delete cluster --name ${self.triggers.cluster_name} --kubeconfig ${self.triggers.kubeconfig_path} || true"
  }

  depends_on = [local_file.kind_config]
}

# Extract API credentials from the kubeconfig kind wrote. We use
# `data.external` (rather than reading the file via `local_file`) so
# the script runs at apply time AFTER the cluster exists; a `local_file`
# data source would try to read the file at plan time.
data "external" "credentials" {
  program = ["bash", "${path.module}/scripts/extract-credentials.sh"]
  query = {
    kubeconfig = var.kubeconfig_path
    context    = "kind-${var.cluster_name}"
  }
  depends_on = [null_resource.cluster]
}
