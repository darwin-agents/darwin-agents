# 30 — Mainnet cutover checklist

Going from the staging deployment (Base Sepolia) to the v1 real-money alpha
(Base mainnet) on Hetzner k3s. Cutover is config, credentials, and process,
not code: staging and prod run the same image, the same Terraform env
(`infra/terraform/envs/hetzner`, `staging.tfvars` vs `prod.tfvars`) and the
same boot guard. Work top to bottom. Every item in §0 must be done before any
real funds move.

The supervisor enforces the dangerous parts itself. Both deployments boot
with `--env=production`, and `_assert_production_ready` refuses a boot that
lacks any of the following, printing every gap at once:

- Clerk auth and an explicit CORS origin;
- live KYC and sanctions;
- LiteLLM virtual keys;
- Vault transit signing;
- Postgres plus leader election;
- the k8s driver;
- CDP, a fee address, the gas tank, E2B, and web push.

A `--network testnet` boot pointed at a mainnet RPC is refused as a mismatch.
This doc is the human superset of that guard.

The v1 money model:

- **Owner money in:** USDC arrives on Base at the Darwin's own address,
  either sent directly or delivered by a MoonPay buy.
- **Revenue:** buyers pay the Darwin through the x402 storefront
  (`POST /store/{id}/report`).
- **Fees:** metering and the harvest fee accrue off-chain and settle to the
  fee address.
- **Owner money out:** a harvest or kill sends funds to the owner's payout
  address (a MoonPay sell uses the provider's deposit wallet).
- **Signing:** pods never sign. The supervisor signs every outbound transfer
  with the Darwin's own CDP account, after a sanctions screen, with gas
  topped up from the gas tank.

---

## 0. Hard gates (process — do NOT skip)

- [ ] **Counsel sign-off.** Counsel must review custody (the platform
      controls each Darwin's CDP account), money transmission, KYC/AML, the
      ToS and privacy policy (`/terms` and `/privacy` ship as drafts), and
      the MoonPay and Persona contracts. This is the biggest gate. Do not
      infer it.
- [ ] **External security audit** done, covering the money path, key custody,
      and tenant isolation ([32 §C2](32-pre-mainnet-gates.md)).
- [ ] **7-day staging soak** green: 3 or more Darwins, zero drift, zero
      crashes, with settlement and harvest exercised ([32 §D2](32-pre-mainnet-gates.md)).
- [ ] **`e2e-staging` green** on the exact image you will promote.
- [ ] **Drills** rehearsed on staging: leader failover, PITR restore, and a
      Vault seal/unseal.

## 1. Accounts and credentials (prod values on the `hetzner-prod` Environment)

Secrets go in GitHub Environment secrets. The deploy workflow composes them
into `TF_VAR_secret_env`, and they reach the supervisor's env Secret from
there. Nothing is committed. The full list is in the header of
`.github/workflows/deploy-hetzner.yml`.

- [ ] **CDP:** create a mainnet project and set `CDP_API_KEY_ID`,
      `CDP_API_KEY_SECRET`, and `CDP_WALLET_SECRET`. Every Darwin's account
      lives under this project.
- [ ] **Gas tank:** generate a fresh key (`DARWIN_GAS_TANK_KEY`) and fund
      it with a small amount of Base ETH. It is a hot key: keep the balance
      small. The supervisor pages `gas_tank_low` when it can fund fewer than
      20 top-ups.
- [ ] **Fee address:** set `PLATFORM_FEE_ADDRESS` to a checksummed address
      you control, ideally a multisig. It is not the gas tank.
- [ ] **RPC:** set `DARWIN_RPC_URL` to a Base mainnet RPC with an archive or
      log-query plan. It is a secret because provider URLs embed a key.
- [ ] **Sanctions:** a Chainalysis (or TRM) key in `DARWIN_SANCTIONS_API_KEY`.
- [ ] **KYC:** a Persona production API key, template id, and webhook
      secret. Register the webhook URL
      `https://<api>/platform/kyc/webhook` in Persona.
- [ ] **MoonPay:** live `MOONPAY_API_KEY`, `MOONPAY_SECRET_KEY`, and
      `MOONPAY_WEBHOOK_KEY`. Enable `usdc_base` on the account.
- [ ] **Clerk:** a production instance. Set `CLERK_ISSUER` here and the
      `pk_live`/`sk_live` keys in Vercel. Restrict sign-ups to an allowlist
      during the canary.
- [ ] **LiteLLM:** provider keys (`CLAUDE_API_KEY`, …), `LITELLM_MASTER_KEY`,
      and `LITELLM_DATABASE_URL` (a `litellm` database on the CNPG cluster).
- [ ] **E2B:** an API key. **Browserbase:** an API key, if browsing is on.
- [ ] **Push:** a VAPID key pair (`DARWIN_VAPID_PUBLIC_KEY` as a variable,
      `DARWIN_VAPID_PRIVATE_KEY` as a secret) and `VAPID_SUBJECT`.
- [ ] **Paging:** `DARWIN_PAGERDUTY_ROUTING_KEY`. Fleet alerts page on
      `drive_crashed`, `ledger_drift`, `settlement_failed`,
      `close_out_failed`, `gas_tank_low`, and `llm_spend_drift`.
- [ ] **Infra:** Hetzner API token, SSH keypair, Object Storage keys, the
      state bucket, `ALLOWED_ADMIN_CIDRS` (`0.0.0.0/0` is refused), and
      `PG_BACKUP_PATH`/`PG_BACKUP_ENDPOINT`.

## 2. Deploy

1. **Bootstrap.** The first time only, dispatch `deploy-hetzner` with
   `environment=prod`, `bootstrap`, and `auto_approve`. This creates the
   cluster and Vault, but not the supervisor.
2. **Vault runbook.** Follow the [Hetzner env README §4](../../infra/terraform/envs/hetzner/README.md#4-vault-runbook-raft-no-kms--manual-unseal):
   1. Run `init` and unseal.
   2. Create the KV mount.
   3. Create the transit key `darwin-platform` (ed25519).
   4. Write the `darwin-kv` and `darwin-sign` policies and bind them to the
      supervisor's ServiceAccount role `darwin`.
   5. Revoke root.
3. **DNS.** Point the API A record at the LoadBalancer IP.
4. **Full deploy.** Dispatch `deploy-hetzner` with `environment=prod` and the
   image tag that passed `e2e-staging`. `hetzner-prod` requires a reviewer.
5. **Dashboard.** Deploy it on Vercel with the production env
   ([web/README.md](../../web/README.md#deploy-vercel)), and add the exact
   Vercel domain to `CORS_ORIGINS`.
6. **Uptime probe.** Point an external probe at `https://<api>/healthz`. It
   pages when the API, ingress, or node is down, which the supervisor cannot
   report about itself.

## 3. Canary (`prod.tfvars` ships the canary posture)

- [ ] **CDP caps:** `cdp_per_tx_usdc = 25` and `cdp_per_day_usdc = 100`,
      enforced at the CDP account, independent of our code.
- [ ] **Store price:** `store_price_usdc = 0.50`.
- [ ] **Platform-owned Darwins only** at first. Fund each with $20–50 and
      watch it tick, reconcile, sell a report, settle, harvest, and get
      killed and swept. Check each transfer on Basescan against the audit log
      (`pod_transfer` rows).
- [ ] **Five alpha users** from the Clerk allowlist. Raise the caps and the
      price only after a clean week.

## 4. Verify

- [ ] The boot did not refuse (supervisor logs). `/readyz` returns 200.
- [ ] `scripts/real_cloud_iac_verify.sh --env hetzner` returns PASS for PITR
      and Vault.
- [ ] Each sanctions screen and transfer appears in the audit log.
- [ ] Zero `ledger.reconciliation_failed` events, and no `ledger_drift` page.

---

Off in prod for v1, but built:

- **Reproduction** (`pod_reproduction = true` → `--pod-reproduction`). The
  supervisor re-verifies the parent's DNA, signs the child, funds it on-chain
  from the parent's account (at most one child per parent per day), and
  spawns it for the same owner. Turn it on only once the audit has covered it.
- **Live self-modification** (`--soma-root`). Without it, evolution stays
  simulated, and no LLM-authored code is loaded anywhere.

Out of scope for v1, and refused or absent in production:

- Stripe card deposits: the platform never holds fiat.
- The omnibus wallet and `--allow-manual-credit`.
- Solana and Stellar.
- Darwin outbound x402 payments.

See [`BACKLOG.md`](BACKLOG.md).
