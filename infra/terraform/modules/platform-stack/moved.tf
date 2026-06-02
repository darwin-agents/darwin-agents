# Vault gained a `count` (dev vs raft selection), which changes its state
# address from module.vault → module.vault[0]. Tell Terraform it moved so an
# existing (pre-vault_mode) stack migrates in place instead of destroying and
# recreating the dev Vault on the next apply.
moved {
  from = module.vault
  to   = module.vault[0]
}
