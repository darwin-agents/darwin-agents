/**
 * cluster-hetzner-k3s — provisions a k3s cluster on Hetzner Cloud servers,
 * prepared for the Hetzner cloud-controller-manager (external cloud
 * provider) and with gVisor (runsc) installed on every node.
 *
 * Output contract MIRRORS modules/kind-cluster exactly (endpoint,
 * cluster_ca_certificate, client_certificate, client_key, kubeconfig_path,
 * cluster_name) so an env wires the helm/kubernetes/kubectl providers
 * identically regardless of provider. Extra outputs (network_id/name)
 * feed the Hetzner CCM.
 */

variable "cluster_name" {
  type        = string
  description = "Cluster name; prefixes hcloud resources and names the kubeconfig context."
  default     = "darwin"
}

variable "location" {
  type        = string
  description = "Hetzner location for the servers. cax (ARM) lives only in nbg1/fsn1/hel1 (eu-central)."
  default     = "nbg1"

  validation {
    condition     = contains(["nbg1", "fsn1", "hel1"], var.location)
    error_message = "cax (ARM) servers exist only in nbg1/fsn1/hel1 (eu-central)."
  }
}

variable "network_zone" {
  type        = string
  description = "Network zone for the private subnet (nbg1/fsn1/hel1 all live in eu-central)."
  default     = "eu-central"
}

variable "server_type" {
  type        = string
  description = "Hetzner server type. cax21 = ARM64 Ampere, 4 vCPU / 8GB. The published image is multi-arch so arm64 runs natively."
  default     = "cax21"
}

variable "image" {
  type        = string
  description = "OS image. hcloud images are multi-arch; the cax line serves the arm64 variant. k3s auto-detects aarch64."
  default     = "ubuntu-24.04"
}

variable "agent_count" {
  type        = number
  description = "Number of k3s agent (worker) nodes, in addition to the single control-plane."
  default     = 2
}

variable "k3s_channel" {
  type        = string
  description = "k3s install channel (INSTALL_K3S_CHANNEL). >=1.32 ships Traefik v3 which we disable in favour of ingress-nginx."
  default     = "v1.32"
}

variable "gvisor_release" {
  type        = string
  description = "Pinned gVisor (runsc) release date for reproducible nodes (https://storage.googleapis.com/gvisor/releases/release/<date>/aarch64). Use 'latest' to track."
  default     = "20260525"
}

# -- Kata Containers (true microVMs, doc 17) --------------------------------
# gVisor (above) is the DEFAULT per-Darwin isolation: a userspace kernel that
# needs no nested virtualization, so it runs on stock Hetzner Cloud VMs. Kata
# gives a stronger boundary — each pod in its own hardware-virtualized microVM
# (QEMU) — but REQUIRES nested virtualization (/dev/kvm), which Hetzner Cloud
# shared/dedicated vCPU instances do NOT expose. Enable this ONLY on
# nested-virt-capable nodes (bare metal, or a provider/instance type with KVM
# passthrough). When enabled, set the platform-stack runtime_class="kata" +
# runtime_handler="kata" so per-Darwin pods schedule onto the microVM runtime.
variable "kata_enabled" {
  type        = bool
  description = "Install Kata Containers + register the containerd `kata` runtime handler on every node. Requires nested virtualization (/dev/kvm). Default off → gVisor only."
  default     = false
}

variable "kata_release" {
  type        = string
  description = "Pinned kata-containers release tag for the kata-static bundle (https://github.com/kata-containers/kata-containers/releases). Only used when kata_enabled."
  default     = "3.21.0"
}

variable "network_cidr" {
  type        = string
  description = "CIDR for the private Hetzner network."
  default     = "10.0.0.0/16"
}

variable "subnet_cidr" {
  type        = string
  description = "CIDR for the private subnet the nodes attach to. Node detection matches the NIC carrying a 10.x address."
  default     = "10.0.0.0/24"
}

variable "cluster_cidr" {
  type        = string
  description = "Pod CIDR for k3s flannel. Must match the CCM's networking.clusterCIDR. k3s default is 10.42.0.0/16."
  default     = "10.42.0.0/16"
}

variable "domain" {
  type        = string
  description = "Optional API domain added to the k3s --tls-san list so the kubeconfig (and cert) work for the domain. Empty = public IP only."
  default     = ""
}

variable "allowed_admin_cidrs" {
  type        = list(string)
  description = "Source CIDRs allowed to reach SSH (22) and the kube API (6443) on the public NIC. Lock this down to your IP."
  default     = ["0.0.0.0/0"]
}

variable "kubeconfig_path" {
  type        = string
  description = "Absolute path where the fetched+rewritten kubeconfig is written."
}

variable "ssh_public_key" {
  type        = string
  description = "Public SSH key contents authorized on every node (for the remote-exec kubeconfig fetch)."
}

variable "ssh_private_key" {
  type        = string
  description = "Matching private SSH key contents, used by Terraform's remote-exec/scp to fetch the kubeconfig."
  sensitive   = true
}
