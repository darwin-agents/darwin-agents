/**
 * hcloud-bootstrap — the Hetzner-specific in-cluster addons that make a k3s
 * cluster usable: the cloud-controller-manager (clears the
 * node.cloudprovider.kubernetes.io/uninitialized taint + provisions
 * LoadBalancers) and the CSI driver (the `hcloud-volumes` StorageClass).
 *
 * Both read one Secret named `hcloud` in kube-system. Must apply BEFORE the
 * platform stack — until the CCM runs, every node is tainted unschedulable.
 */

variable "hcloud_token" {
  type        = string
  description = "Hetzner Cloud API token (Read+Write) for the CCM + CSI."
  sensitive   = true
}

variable "network_name" {
  type        = string
  description = "Private Hetzner network name/ID — the CCM's `network` (required when networking.enabled)."
}

variable "cluster_cidr" {
  type        = string
  description = "Pod CIDR — must equal the k3s --cluster-cidr (k3s flannel default 10.42.0.0/16). The CCM chart default (10.244.0.0/16) is wrong for k3s."
  default     = "10.42.0.0/16"
}

variable "lb_location" {
  type        = string
  description = "Default Hetzner location for CCM-provisioned LoadBalancers."
  default     = "nbg1"
}

variable "ccm_chart_version" {
  type        = string
  description = "hcloud-cloud-controller-manager chart version."
  default     = "1.31.1"
}

variable "csi_chart_version" {
  type        = string
  description = "hcloud-csi chart version (creates the hcloud-volumes StorageClass)."
  default     = "2.21.2"
}
