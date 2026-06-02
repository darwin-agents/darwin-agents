/**
 * k3s on Hetzner Cloud.
 *
 *   network + subnet + firewall  → private plane for the nodes
 *   random_password              → k3s join token (known before boot)
 *   control_plane + agents       → cloud-init installs gVisor then k3s
 *   null_resource.fetch          → SSH in, wait for readyz, scp kubeconfig,
 *                                  rewrite 127.0.0.1 → public IP
 *   data.external.credentials    → decode kubeconfig base64 → PEM
 *
 * The hcloud provider is configured by the calling env (root) and inherited
 * here. Outputs mirror modules/kind-cluster so envs wire providers the same.
 */

locals {
  cp_private_ip     = cidrhost(var.subnet_cidr, 2) # 10.0.0.2
  agent_private_ips = [for i in range(var.agent_count) : cidrhost(var.subnet_cidr, 3 + i)]

  node_common = templatefile("${path.module}/templates/node-common.sh.tftpl", {
    gvisor_release = var.gvisor_release
  })

  cp_user_data = templatefile("${path.module}/templates/server.sh.tftpl", {
    node_common  = local.node_common
    k3s_channel  = var.k3s_channel
    k3s_token    = random_password.k3s_token.result
    cluster_cidr = var.cluster_cidr
    domain       = var.domain
  })
}

resource "random_password" "k3s_token" {
  length  = 48
  special = false
}

resource "hcloud_ssh_key" "admin" {
  name       = "${var.cluster_name}-admin"
  public_key = var.ssh_public_key
}

resource "hcloud_network" "this" {
  name     = "${var.cluster_name}-net"
  ip_range = var.network_cidr
}

resource "hcloud_network_subnet" "this" {
  network_id   = hcloud_network.this.id
  type         = "cloud"
  network_zone = var.network_zone
  ip_range     = var.subnet_cidr
}

# Firewalls apply to the PUBLIC NIC only; private-net traffic is unfiltered.
resource "hcloud_firewall" "this" {
  name = "${var.cluster_name}-fw"

  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "22"
    source_ips = var.allowed_admin_cidrs
  }
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "6443"
    source_ips = var.allowed_admin_cidrs
  }
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "80"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "443"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
  rule {
    direction  = "in"
    protocol   = "icmp"
    source_ips = var.allowed_admin_cidrs
  }
}

resource "hcloud_server" "control_plane" {
  name         = "${var.cluster_name}-cp"
  server_type  = var.server_type
  image        = var.image
  location     = var.location
  ssh_keys     = [hcloud_ssh_key.admin.id]
  firewall_ids = [hcloud_firewall.this.id]
  user_data    = local.cp_user_data
  labels       = { cluster = var.cluster_name, role = "control-plane" }

  network {
    network_id = hcloud_network.this.id
    ip         = local.cp_private_ip
  }

  depends_on = [hcloud_network_subnet.this]
}

resource "hcloud_server" "agent" {
  count        = var.agent_count
  name         = "${var.cluster_name}-agent-${count.index}"
  server_type  = var.server_type
  image        = var.image
  location     = var.location
  ssh_keys     = [hcloud_ssh_key.admin.id]
  firewall_ids = [hcloud_firewall.this.id]
  labels       = { cluster = var.cluster_name, role = "agent" }

  user_data = templatefile("${path.module}/templates/agent.sh.tftpl", {
    node_common   = local.node_common
    k3s_channel   = var.k3s_channel
    k3s_token     = random_password.k3s_token.result
    cp_private_ip = local.cp_private_ip
  })

  network {
    network_id = hcloud_network.this.id
    ip         = local.agent_private_ips[count.index]
  }

  depends_on = [hcloud_server.control_plane]
}

# Wait for the API server, then fetch + rewrite the kubeconfig to the public IP.
resource "null_resource" "fetch_kubeconfig" {
  triggers = {
    cp_id = hcloud_server.control_plane.id
    path  = var.kubeconfig_path
  }

  provisioner "remote-exec" {
    inline = [
      "until test -f /etc/rancher/k3s/k3s.yaml; do sleep 3; done",
      "until k3s kubectl get --raw=/readyz >/dev/null 2>&1; do sleep 3; done",
    ]
    connection {
      type        = "ssh"
      host        = hcloud_server.control_plane.ipv4_address
      user        = "root"
      private_key = var.ssh_private_key
      timeout     = "10m"
    }
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      keyfile="$(mktemp)"; trap 'rm -f "$keyfile"' EXIT
      printf '%s' "$SSH_KEY" > "$keyfile"; chmod 600 "$keyfile"
      scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$keyfile" \
        root@${hcloud_server.control_plane.ipv4_address}:/etc/rancher/k3s/k3s.yaml \
        ${var.kubeconfig_path}
      # The kubeconfig from k3s points at 127.0.0.1; rewrite to the public IP
      # (valid because the IP is in --tls-san). k3s names the context 'default'.
      sed -i "s#https://127.0.0.1:6443#https://${hcloud_server.control_plane.ipv4_address}:6443#" ${var.kubeconfig_path}
    EOT
    environment = { SSH_KEY = var.ssh_private_key }
  }

  depends_on = [hcloud_server.agent]
}

# Decode the kubeconfig base64 → PEM (reuses the kind module's script verbatim).
data "external" "credentials" {
  program = ["bash", "${path.module}/scripts/extract-credentials.sh"]
  query = {
    kubeconfig = var.kubeconfig_path
    context    = "default" # k3s names its context 'default'
  }
  depends_on = [null_resource.fetch_kubeconfig]
}
