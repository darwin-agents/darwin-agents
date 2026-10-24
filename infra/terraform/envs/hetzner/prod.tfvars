# Production: Base mainnet. Identities come from the `hetzner-prod` GitHub
# environment's variables, secrets via TF_VAR_secret_env.
cluster_name       = "darwin"
agent_count        = 2
network            = "mainnet"
database_instances = 2
fiat_provider      = "moonpay"

# Canary posture (doc 32 C5): low price, tight CDP caps. Raise after the
# canary gate passes.
store_price_usdc = 0.50
cdp_per_tx_usdc  = 25
cdp_per_day_usdc = 100

database_backup_enabled               = true
database_backup_s3_credentials_secret = "pg-backup-s3"
# database_backup_destination_path / _endpoint_url come from the GitHub
# environment's variables; the keys via TF_VAR_database_backup_s3_credentials.
