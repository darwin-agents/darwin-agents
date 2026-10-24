# Staging: Base Sepolia, same boot guard as prod (--env=production).
# Deployment identities (domain, Clerk issuer, CORS origins, fee address,
# admin CIDRs) come from the `hetzner-staging` GitHub environment's
# variables, secrets via TF_VAR_secret_env — see .github/workflows/deploy-hetzner.yml.
cluster_name       = "darwin-staging"
agent_count        = 1
network            = "testnet"
database_instances = 1
fiat_provider      = "moonpay" # MoonPay sandbox keys in this environment
store_price_usdc   = 0.05
acme_server        = "https://acme-staging-v02.api.letsencrypt.org/directory"
