# 30 — Mainnet cutover checklist

Going from the Base Sepolia testnet stack to a real-money production deploy.
The platform is network-agnostic by construction; cutover is a config +
credentials + process exercise, not a code rewrite. Work top to bottom — the
**Hard gates** must all pass before any real funds move.

The supervisor enforces the dangerous parts itself: booting against mainnet
with demo-grade money config is refused (`_assert_mainnet_safe`), and the
preflight (`scripts/real_preflight.sh --network mainnet`) mirrors those
checks. This doc is the human-facing superset.

---

## 0. Pre-conditions (process — do NOT skip)

These are not code; they gate whether you may legally run this at all.

- [ ] **Legal/regulatory sign-off** for autonomous agents holding & moving
      real user funds (money-transmission, KYC/AML, custody, liability).
      This is the single biggest gate. Get counsel; do not infer it.
- [ ] **External security audit** completed (release-gate red-team CI is
      necessary, not sufficient).
- [ ] **7-day testnet soak** green (`make real` on Sepolia, real LLM/wallet).
- [ ] **Operational drills** rehearsed on Postgres + Vault: leader failover,
      DB restore-from-backup, key rotation.

## 1. Chains → mainnet

- [ ] `DARWIN_RPC_URL` → Base mainnet RPC (no `sepolia`).
- [ ] Boot with **`--network mainnet`** — the single authoritative switch.
      CDP wallets flip to `base-mainnet` and the USDC contract + chain id
      follow automatically; the money-safety gate and preflight key off the
      same flag, so no subsystem can silently disagree. (`--cdp-mainnet` is a
      kept-for-back-compat alias.) A `--network testnet` boot pointed at a
      mainnet RPC is refused as a mismatch.
- [ ] If using Solana/Stellar: `DARWIN_SOLANA_RPC_URL` →
      `api.mainnet-beta.solana.com`, `DARWIN_STELLAR_HORIZON_URL` →
      `horizon.stellar.org`. Use `--wallet-provider cdp` or `local-vault` —
      the `web3` shared-key provider **refuses mainnet by design**.
- [ ] Fund the **omnibus** mainnet address with real USDC + native gas (no
      faucet on mainnet). Omnibus credit is persisted (schema ≥ v7) but must
      be backed by real on-chain USDC.

## 2. Compliance (hard gates on mainnet)

- [ ] **Sanctions**: `--sanctions-provider chainalysis|trm` + a real
      `DARWIN_SANCTIONS_API_KEY`. The offline `local` denylist is **refused**
      on mainnet (can't catch a freshly-listed address).
- [ ] **KYC**: real `--kyc-provider persona|onfido` with live keys +
      template/workflow id (required for fiat deposits & withdrawals).
- [ ] **Manual credit endpoint OFF**: do **not** pass `--allow-manual-credit`
      (it mints unbacked credit; the boot refuses it on mainnet anyway).
      Credit enters only via the Stripe webhook and verified on-chain deposits.

## 3. Fiat (if enabled)

- [ ] `STRIPE_API_KEY` → live `sk_live_…` (preflight rejects `sk_test_` on
      mainnet).
- [ ] `STRIPE_WEBHOOK_SECRET` set + `--stripe-webhook-secret` wired; register
      the `POST /webhooks/stripe` endpoint URL in the Stripe dashboard for
      `payment_intent.succeeded`.
- [ ] Stripe **Connect** merchant onboarding done for any withdrawal
      destinations (the withdraw path assumes connected accounts).

## 4. LLM

- [ ] `litellm.config.yaml` uses production tiers (reasoning → Sonnet,
      coding → Opus) — **not** the Haiku local-dev override.
- [ ] Live provider keys held by the proxy; Redis cache reachable.

## 5. Secrets & auth

- [ ] All secrets delivered via k8s `Secret` / Helm `existingSecret` — **not**
      a committed/shipped `.env`. Rotate every key carried through testnet.
- [ ] **Vault**: AppRole or k8s-auth (not a root token); rotation scheduled.
- [ ] **Auth**: Clerk JWT (`--clerk-issuer` + `--clerk-jwks-url`); retire the
      static `tokens.json` dev path.
- [ ] TLS termination + HTTP rate-limiting provisioned at the edge
      (reverse proxy / Cloudflare / ALB).

## 6. Infra / HA

- [ ] Postgres (`--db postgresql://…`) + `--leader-election` across ≥2
      replicas; re-enable the PodDisruptionBudget; consider HPA.
- [ ] Hosted, persistent observability (Prometheus/Jaeger/Grafana) — not the
      local docker-compose stack.
- [ ] Image pinned to a `sha-…` tag (not `latest`); web dashboard built &
      hosted.

## 7. Verify (low float first)

- [ ] `make real-preflight NETWORK=mainnet` → PASS.
- [ ] Boot; confirm it did **not** refuse (the gate is satisfied).
- [ ] Spawn one Darwin with a small opening (e.g. $20–50), watch it tick,
      reconcile, and harvest. Confirm a real fiat deposit lands via the
      webhook and a withdrawal settles.
- [ ] Run a real on-chain transfer to a known-good address; confirm the
      sanctions screener is actually consulted (check the audit log).

---

**Webhook idempotency** is now two-layer + durable: a `processed_webhook_events`
ledger short-circuits a re-delivered Stripe event id before any money effect,
and quote-consumption catches a crash between credit and mark — both survive
restarts. See `docs/engineering/BACKLOG.md` for explicitly out-of-scope items.
