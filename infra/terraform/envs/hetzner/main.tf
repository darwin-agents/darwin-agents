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
 * One env, two deployments: `staging.tfvars` (Base Sepolia) and
 * `prod.tfvars` (Base mainnet), each with its own state key. Both boot with
 * `--env=production`, so staging runs the same boot guard
 * (`_assert_production_ready`) prod does, just on testnet money.
 *
 * Remote state lives in Hetzner Object Storage — see backend.tf.example.
 * Post-apply steps (DNS, Vault init/unseal, gVisor verify) are in README.md.
 */

locals {
  # The supervisor flags the production boot guard checks that the chart
  # doesn't already set (--driver, --db, --wallet-provider, --leader-election
  # come from chart values). Secrets arrive as env via var.secret_env.
  platform_args = concat(
    [
      "--env=production",
      "--network=${var.network}",
      "--clerk-issuer=${var.clerk_issuer}",
      "--kyc-provider=${var.kyc_provider}",
      "--sanctions-provider=${var.sanctions_provider}",
      "--litellm-virtual-keys",
      "--signing-vault-transit-key=${var.signing_vault_transit_key}",
      # Vault as the supervisor's ServiceAccount (role from README §4).
      "--vault-k8s-role=darwin",
      "--platform-fee-address=${var.platform_fee_address}",
      "--push-provider=webpush",
      "--vapid-subject=${var.vapid_subject}",
      "--fiat-provider=${var.fiat_provider}",
    ],
    [for o in var.cors_origins : "--cors-origin=${o}"],
    var.store_price_usdc == null ? [] : ["--store-price-usdc=${var.store_price_usdc}"],
    var.x402_facilitator_url == "" ? [] : ["--x402-facilitator-url=${var.x402_facilitator_url}"],
    var.cdp_per_tx_usdc == null ? [] : ["--cdp-per-tx-usdc=${var.cdp_per_tx_usdc}"],
    var.cdp_per_day_usdc == null ? [] : ["--cdp-per-day-usdc=${var.cdp_per_day_usdc}"],
    var.pod_reproduction ? ["--pod-reproduction"] : [],
  )
}

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
  database_instances    = var.database_instances

  # Vault auto-unseal (doc 20, README §4b). Empty = manual unseal (default).
  vault_seal_config_hcl = var.vault_seal_config_hcl
  vault_seal_env        = var.vault_seal_env

  # Postgres PITR backup (doc 20, README §6). Off by default.
  database_backup_enabled               = var.database_backup_enabled
  database_backup_destination_path      = var.database_backup_destination_path
  database_backup_endpoint_url          = var.database_backup_endpoint_url
  database_backup_s3_credentials_secret = var.database_backup_s3_credentials_secret
  database_backup_s3_credentials        = var.database_backup_s3_credentials
  database_backup_retention_policy      = var.database_backup_retention_policy

  # gVisor isolation: nodes carry the runsc shim (cloud-init); the chart
  # creates the RuntimeClass named "gvisor" → handler runsc.
  runtime_class        = "gvisor"
  create_runtime_class = true
  runtime_handler      = "runsc"

  wallet_provider = var.wallet_provider
  leader_election = true
  extra_args      = local.platform_args
  secret_env      = var.secret_env

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
