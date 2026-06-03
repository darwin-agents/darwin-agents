/**
 * Hetzner Cloud Darwin platform environment (k3s).
 *
 * Thin provider env over the cloud-agnostic `platform-stack` module — the
 * cloud twin of envs/local. Only the cluster module + provider knobs differ;
 * platform-stack itself is identical.
 *
 *   1. module.cluster           — hcloud servers + k3s + gVisor (runsc) nodes
 *   2. helm/k8s/kubectl         — providers wired from the cluster outputs
 *   3. module.hcloud_bootstrap  — CCM (clears node taint + LBs) + CSI (storage)
 *   4. module.platform_stack    — the shared stack with CLOUD knobs:
 *                                 raft Vault, gVisor, hcloud-volumes storage,
 *                                 LoadBalancer ingress, cert-manager TLS
 *
 * Remote state lives in Hetzner Object Storage — see backend.tf.example.
 * Post-apply steps (DNS, Vault init/unseal, gVisor verify) are in README.md.
 */

provider "hcloud" {
  token = var.hcloud_token
}

module "cluster" {
  source = "../../modules/cluster-hetzner-k3s"

  cluster_name        = var.cluster_name
  location            = var.location
  server_type         = var.server_type
  agent_count         = var.agent_count
  domain              = var.domain
  allowed_admin_cidrs = var.allowed_admin_cidrs
  kubeconfig_path     = "${path.module}/.kubeconfig"
  ssh_public_key      = var.ssh_public_key
  ssh_private_key     = var.ssh_private_key

  # Kata true-microVM runtime on nodes (doc 17). Off by default → gVisor
  # only; requires nested virt. See README §5b.
  kata_enabled = var.kata_enabled
  kata_release = var.kata_release
}

# Inline creds from the cluster outputs (unknown at plan, resolved at apply) —
# same eager-config-validation dodge as envs/local.
provider "helm" {
  kubernetes {
    host                   = module.cluster.endpoint
    cluster_ca_certificate = module.cluster.cluster_ca_certificate
    client_certificate     = module.cluster.client_certificate
    client_key             = module.cluster.client_key
  }
}

provider "kubernetes" {
  host                   = module.cluster.endpoint
  cluster_ca_certificate = module.cluster.cluster_ca_certificate
  client_certificate     = module.cluster.client_certificate
  client_key             = module.cluster.client_key
}

provider "kubectl" {
  host                   = module.cluster.endpoint
  cluster_ca_certificate = module.cluster.cluster_ca_certificate
  client_certificate     = module.cluster.client_certificate
  client_key             = module.cluster.client_key
  load_config_file       = false
  lazy_load              = true
}

# Hetzner in-cluster addons: clears the node.cloudprovider uninitialized taint
# (CCM) and provides the hcloud-volumes StorageClass (CSI). MUST precede the
# platform stack — until the CCM runs, nodes are unschedulable.
module "hcloud_bootstrap" {
  source = "../../modules/hcloud-bootstrap"

  hcloud_token = var.hcloud_token
  network_name = module.cluster.network_name
  lb_location  = var.location

  depends_on = [module.cluster]
}

module "platform_stack" {
  source = "../../modules/platform-stack"

  namespace          = "darwin"
  platform_image     = var.platform_image
  orchestrator_image = var.platform_image
  image_pull_policy  = "IfNotPresent"

  # Production secrets: raft Vault (manual unseal — see README runbook).
  vault_mode            = "raft"
  storage_class         = module.hcloud_bootstrap.storage_class # hcloud-volumes
  database_storage_size = "10Gi"
  database_instances    = 1 # bump to >=3 for HA Postgres

  # Vault auto-unseal (doc 20, README §4b). Empty = manual unseal (default).
  vault_seal_config_hcl = var.vault_seal_config_hcl
  vault_seal_env        = var.vault_seal_env

  # Postgres PITR backup (doc 20, README §6). Off by default.
  database_backup_enabled               = var.database_backup_enabled
  database_backup_destination_path      = var.database_backup_destination_path
  database_backup_endpoint_url          = var.database_backup_endpoint_url
  database_backup_s3_credentials_secret = var.database_backup_s3_credentials_secret
  database_backup_retention_policy      = var.database_backup_retention_policy

  # gVisor isolation: nodes carry the runsc shim (cloud-init); the chart
  # creates the RuntimeClass named "gvisor" → handler runsc.
  runtime_class        = "gvisor"
  create_runtime_class = true
  runtime_handler      = "runsc"

  wallet_provider = var.wallet_provider

  litellm_master_key    = var.litellm_master_key
  litellm_provider_keys = var.litellm_provider_keys
  litellm_config_yaml   = file("${path.module}/litellm.config.yaml")
  litellm_database_url  = var.litellm_database_url

  # LoadBalancer exposure + Let's-Encrypt TLS on the real domain.
  exposure             = "loadbalancer"
  lb_location          = var.location
  ingress_host         = var.domain
  tls_enabled          = true
  cluster_issuer       = "letsencrypt-prod"
  tls_secret_name      = "${replace(var.domain, ".", "-")}-tls"
  cert_manager_install = true
  acme_email           = var.acme_email
  acme_server          = var.acme_server

  depends_on = [module.hcloud_bootstrap]
}
