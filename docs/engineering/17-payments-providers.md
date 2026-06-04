# Payments — providers

This complements [`05-treasury-and-payments.md`](05-treasury-and-payments.md) with concrete provider choices.

## The stack

```
                           USER (KYC'd, verified)
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                                       ▼
        Stripe                                   MoonPay
        (card / ACH / SEPA)                      (crypto-native users)
              │                                       │
              └───────────────────┬───────────────────┘
                                  ▼
                       Platform omnibus wallet
                        (USDC on Base, custodial)
                                  │
                  ┌───────────────┴────────────────┐
                  │ on launch / top-up             │ on withdraw
                  ▼                                ▲
        Per-Darwin Agentic Wallet (Coinbase)       │
        (USDC on Base, non-custodial TEE,          │
        programmable spend policy)                 │
                                  │
                  ┌───────────────┼────────────────┐
                  ▼               ▼                ▼
               x402 APIs    Direct USDC sends   AgentCore Payments
               (machine-    (to known           (AWS-mediated, used
                to-machine)  counterparties)    for some merchants)
```

## Why USDC on Base

- **USDC** is the most widely-supported regulated stablecoin and the default for Coinbase Agentic Wallets and x402.
- **Base** has near-zero gas, fast finality, native Coinbase support, and is the most-deployed x402 settlement chain (85% of x402 volume by April 2026).
- Fallback chain: Solana for higher TPS bursts; Stellar for ultra-cheap micropayments. We don't enable these in v1 but x402 V2 supports multi-chain, so adding them is a config change.

## Per-Darwin wallet provisioning — three backends

A Darwin's wallet is selected fleet-wide at `--wallet-provider {cdp,local-vault,web3}`. The supervisor forwards the choice into each per-Darwin orchestrator pod so every Darwin in the fleet uses the same backend.

**`cdp` — Coinbase CDP / Agentic Wallet (production default).** Each Darwin gets its own non-custodial CDP account, named `darwin-<id>`. `get_or_create_account` is idempotent on the name, so resume after a process restart re-binds to the same on-chain address. Properties:

- **TEE-secured key material** — Coinbase holds the private key inside its TEE; this process only carries API credentials. Neither the platform nor Coinbase can drain the wallet without the per-Darwin signing flow.
- **Per-wallet spending limits** enforced at the wallet level *in addition* to the genome's policy engine — defense in depth.
- **Gasless transactions** on Base; no gas reserve to manage.
- **CDP Portal integration** for operator-side telemetry.

Activate with `--wallet-provider cdp` plus `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` / `CDP_WALLET_SECRET`. Default network is base-sepolia; `--cdp-mainnet` opts into base-mainnet. Requires the `[cdp]` extra.

**`local-vault` — self-custodial per-Darwin keypair sealed in the vault.** For chains CDP doesn't cover (Solana, Stellar) or operators who don't want a CDP dependency. On first spawn the platform mints a fresh keypair per Darwin via the configured `WalletKeypairMinter`, seals the secret in the Darwin's vault namespace at `sensitive/wallet/<chain>/secret`, and constructs the per-chain `ChainClient` against it. Subsequent spawns (resume) read the sealed secret. Properties:

- **No platform-level private key in `.env`** — only RPC URLs. Each Darwin's address is distinct.
- **Sensitive-tier vault retention** — the secret falls under the 90-day-after-last-use sweep (doc 16), but the wallet reads it every heartbeat so the TTL effectively never elapses while the Darwin is alive.
- **Custody trade-off** — a vault you hold keys in is itself a custody system. Leak the vault and you've leaked every Darwin's funds. Acceptable for testnet and Solana/Stellar where CDP isn't an option; for EVM mainnet, prefer `cdp`.

Activate with `--wallet-provider local-vault` plus `--chain-networks` (e.g. `base,solana,stellar`) and the matching RPC URLs (`DARWIN_RPC_URL` / `DARWIN_SOLANA_RPC_URL` / `DARWIN_STELLAR_HORIZON_URL`). Each chain needs its corresponding extra installed (`[web3]`, `[solana]`, `[stellar]`).

**`web3` — single shared private key (testnet dev shortcut).** One key from `DARWIN_WALLET_KEY` / `DARWIN_SOLANA_WALLET_KEY` / `DARWIN_STELLAR_SECRET` signs for every Darwin on the platform. Every Darwin shares the same address; useful for a single-tenant dev loop or CI fixtures. **Refuses to start when any configured RPC URL contains `mainnet` or points at `horizon.stellar.org`** — a single shared key on mainnet would let any spawned Darwin drain the fleet treasury, so the platform rejects the config before launching. Use only for testnet.

### NamespaceProvisioner integration

When a `NamespaceProvisioner` is wired (doc 22), per-Darwin K8s namespace + Vault namespace + organism Postgres database are provisioned atomically on spawn. The optional `wallet_keypair_minters: dict[str, WalletKeypairMinter]` constructor argument adds a fourth step: mint a fresh keypair per chain, write the secret + address into the per-Darwin vault namespace, surface the address on `TenantNamespace.wallet_addresses` so the supervisor can use it (omnibus settlement destinations, fiat-payout audit). The wallet step rolls back the previous three steps atomically on failure; the audit recorder receives the addresses (never the secrets).

## x402 in production

Most outbound machine-to-machine payments use x402:

- Platform LiteLLM gateway calls (settled Darwin-to-platform in USDC via x402 at reconciliation)
- API calls to third-party data and content services
- Compute purchases (E2B and Modal both expose x402-compatible billing endpoints)
- Agent.market services (when Darwin discovers it needs a capability another agent offers)

Flow per x402 spec:
1. Neuron makes a request to a paid endpoint.
2. Endpoint returns HTTP 402 with price metadata.
3. The neuron's HTTP client (a thin wrapper around Coinbase's x402 client) consults the wallet's spend policy.
4. If allowed, the wallet signs and broadcasts the USDC payment.
5. Endpoint unlocks; the neuron receives the resource.

The whole cycle completes in ~5 seconds on Base. Cost per micropayment: a few cents (the payment itself) plus essentially nothing for the on-chain transaction.

## Fiat at the edges

Users want to deposit dollars/euros and withdraw to bank accounts. We don't ask them to interact with crypto directly:

- **Stripe** for card and ACH deposits. Stripe accepts USD, EUR, GBP, etc., we convert internally to USDC at the omnibus. Stripe also handles refunds.
- **MoonPay** as a secondary provider, especially for users who prefer to deposit crypto directly (BTC, ETH → swap to USDC at the edge). Their February 2026 Agents product is built for our exact use case. MoonPay is a **hosted-widget + webhook** integration (`adapters/fiat.py::MoonPayFiatRamp`), not a synchronous server call: `deposit_quote` returns a **signed** buy-widget URL (HMAC-SHA256 of the URL search string with the secret key; the destination Darwin rides in `externalCustomerId` and the platform quote id in `externalTransactionId`), and on completion MoonPay POSTs a signed webhook to `POST /webhooks/moonpay`. The adapter verifies the `Moonpay-Signature-V2` header against the webhook key and the supervisor credits the destination Darwin's wallet on a `completed` buy (idempotent via the durable webhook-event ledger). `withdraw` returns a signed sell-widget URL and a `pending` receipt that a `sell_transaction` webhook later settles. `confirm_deposit` remains as a server-side poll fallback (look the transaction up by `externalTransactionId`) for a missed webhook.
- **Bank withdrawals** go out via Stripe Connect Express to the user's verified bank.
- **Crypto withdrawals** go to a verified wallet the user has signed a message from.

KYC happens once at the user level, not per-Darwin. We use Persona (or Onfido; final pick on price) at signup; subsequent Darwins under the same user inherit the KYC status.

## Treasury policy engine implementation

The policy engine is a Python module living in the DNA core. Every outbound payment from a Darwin's wallet:

1. The neuron requests payment via `payments.facilitator` neuron.
2. `payments.facilitator` calls the DNA policy engine with the payment intent.
3. Policy engine checks (in order): DNA hard caps → owner soft caps → strategy envelope → escalation rules → anomaly detection.
4. If approved, the engine returns a signed payment intent the wallet SDK can submit.
5. If refused, the engine returns a reason; the orchestrator decides whether to escalate, downgrade, or abandon.

The policy engine never has the private key. It produces *intents* that the wallet SDK turns into transactions. The wallet enforces a second-layer policy (Coinbase Agentic Wallet's built-in limits) — both must approve.

## AWS Bedrock AgentCore Payments — descoped for the MVP

AWS Bedrock AgentCore Payments (launched late 2025; Coinbase + Stripe under the hood) would be a *secondary* payment rail. It is **not integrated for the MVP**, deliberately:

- It would only re-wrap Coinbase and Stripe — both of which the platform already integrates **directly** (the Coinbase CDP wallet and the Stripe fiat ramp). AgentCore is an AWS-shaped abstraction over rails we already have.
- The platform is provider-neutral and runs without an AWS account; taking an AWS dependency for a redundant rail works against a lean, low-cost launch.

Revisit only if a meaningful merchant ecosystem standardises on AgentCore-first checkout — an adapter can then slot in behind the existing `payments.facilitator` / x402 abstraction.

## Compliance

- **Sanctions screening.** Every payment counterparty is screened against OFAC SDN, EU Consolidated, UK HMT lists before disbursement. We use Chainalysis or TRM Labs (final pick on price); their APIs return clean/dirty/sanctioned in <100ms. A flagged counterparty halts the payment and notifies platform compliance, not the user.
- **AML.** We hold an MTL (money transmitter license) or equivalent in the jurisdictions we launch in, or operate through a partner who does. The exact structuring is a legal question outside this doc's scope. See [`../overview/05-legal-and-tos.md`](../overview/05-legal-and-tos.md).
- **Tax reporting.** We hand the owner clean, jurisdiction-neutral records — not filled-in statutory forms. Two exports (`payments/ledger_export.py`): a flat per-movement **CSV** (`GET /darwins/{id}/ledger.csv`) and an **annual summary** (`GET /darwins/{id}/ledger/summary?year=YYYY`) totalling revenue, costs, and harvest events by category for a tax year, per Darwin (owners with several Darwins fold the per-Darwin summaries together via `merge_summaries`). The owner or their accountant/tax tool (TurboTax, Koinly, …) does the actual filing. We deliberately do **not** generate any country's official return (US 1099/Schedule C/D, UK Self Assessment, EU equivalents): that is jurisdiction-specific, changes yearly, and would make the platform a regulated tax preparer — the same reason Coinbase, Stripe, and brokerages hand over records, not forms.
- **Audit trail.** Every payment links back to the originating event log entry; the chain is hash-anchored daily.

## Owner pre-approvals — typed exceptions to escalation

The constitution escalates every freelancer, legal/financial, owner-withdrawal, and over-review-threshold spend by default. That's the right floor for high-friction payment classes, but it makes recurring small-ticket flows onerous (every Upwork escrow payment wakes the owner). A **pre-approval rule** is the owner's pre-committed answer: *for the next 7 days, spends matching these criteria don't need to wake me up — allow them automatically and log the rule that cleared them*.

Rule shape (`PreApprovalRule` in the constitution package):

```python
PreApprovalRule(
    rule_id="upwork-under-50",         # owner-chosen, opaque to the engine
    counterparty_category="freelancer",  # optional; conjunctive match
    counterparty_substring="upwork",     # optional; case-insensitive
    max_amount_usdc=50.0,                # required; per-spend ceiling
    expires_at=1_717_200_000.0,          # optional; epoch seconds
    max_uses=10,                         # optional; None = unlimited
    used_count=0,                        # bumped by consume_preapproval
)
```

Match is conjunctive: every non-`None` field must hold against the `SpendIntent`. A matching rule converts what would have been an `ESCALATE` into an `ALLOW` whose `layer` is `pre_approval:<rule_id>`, audit-logged with the rationale. After a successful pre-approved pay the facilitator calls `wallet.consume_preapproval(rule_id)` which bumps `used_count` and writes through to the persistent store via a callback the supervisor installed at spawn time. Once `used_count >= max_uses` the rule stops matching and subsequent spends fall through to escalation.

Pre-approvals **never** loosen the constitution's hard rules: the forbidden screen (layer 1), DNA hard caps (layer 2), strategy envelope (layer 3), solvency, sanctions screening, and anomaly detection still apply unchanged. A rule with `max_amount_usdc=1_000_000` against a `per_action_usdc=50` hard cap still refuses anything over $50.

The platform manages the rule set via the operator API:

- `PUT  /platform/wallets/{darwin_id}/preapprovals/{rule_id}` — create or replace. Body carries `max_amount_usdc`, `counterparty_category`, `counterparty_substring`, `expires_at`, `max_uses`. Replacing an existing rule resets `used_count` to 0.
- `DELETE /platform/wallets/{darwin_id}/preapprovals/{rule_id}` — revoke (idempotent)
- `GET  /platform/wallets/{darwin_id}/preapprovals` — list active rules

Rules live on the wallet (`set_preapproval` / `revoke_preapproval` / `owner_preapprovals`); the genome's `payments.facilitator` reads them every time it builds the `TreasuryState` so a revoke takes effect on the next policy check. Persistence: rules + `max_uses` + `used_count` round-trip through the `darwin_preapprovals` SQL table (schema v6), so an expired rule is inert without a revoke, and `used_count` survives a supervisor restart. Inheritance: when a parent reproduces, `_inherit_preapprovals` copies the parent's rule set onto the child's wallet so the owner's "Upwork under $50" intent applies to the lineage; the operator can revoke per-child if narrower scope is needed.

## What about a Darwin paying a human?

Some strategies involve hiring humans — pay a freelance writer, pay a translator, tip a community moderator. Three settlement paths, each gated on owner approval (or a matching pre-approval) since the constitution always escalates `counterparty_category="freelancer"`:

**1. Direct USDC to a verified wallet** (low friction, low compliance overhead). The standard `wallet.pay(to, amount_usdc, memo)` path. The Darwin's `payments.facilitator` calls it after the escalation is approved (or after a matching pre-approval clears it directly). On-chain settlement; the recipient sees USDC in their wallet seconds later.

**2. Fiat via Stripe Connect / MoonPay** (higher friction, higher compliance overhead). For freelancers who only take a bank transfer. The platform exposes `POST /darwins/{id}/fiat_payment` (operator-only). Two legs, gated by the freelancer registry:

   - **Registry lookup** — the request body carries `freelancer_id`, not a raw destination. The supervisor resolves it through the `FreelancerRegistry` scoped to the Darwin's `owner_subject`. An id not registered for the requesting owner refuses with a 409 *before* any chain transfer fires — registry membership is the platform's KYC gate at the fiat edge, and the per-owner scoping prevents user A from fiat-routing through user B's onboarded freelancers.
   - **Leg 1** — `wallet.transfer_out_for_fiat_payout(amount, destination=omnibus.address())` debits the Darwin's USDC wallet to the omnibus address on-chain.
   - **Leg 2** — `omnibus.relay_fiat_payout(darwin_id, amount, fiat_destination, fiat_ramp, escalation_id)` calls `fiat_ramp.withdraw(amount_usdc, destination)` which executes the Stripe Connect Transfer (or MoonPay payout) to the freelancer's verified bank account. The omnibus records a `DARWIN_FIAT_PAYOUT` ledger entry with the provider transaction id and the originating `escalation_id` so a forensic timeline ties the approval to the settlement. `user_credits` is untouched — the source is the Darwin's wallet, not a user deposit.

   This is an *operator-initiated* call: the genome can't reach fiat directly. Typical flow: Darwin escalates → owner approves through the escalation API → operator invokes `darwin_fiat_payout(freelancer_id=..., escalation_id=...)` to settle via fiat. The intermediate omnibus hop insulates the Darwin's wallet from the ramp's settlement timing and gives the operator a clean reversal point if the fiat leg fails.

   **Failure recovery (leg 2 fails after leg 1 succeeded).** When `fiat_ramp.withdraw` throws, the Darwin has already lost USDC to the omnibus address. `POST /darwins/{id}/fiat_payment/refund` calls `omnibus.refund_to_darwin(darwin_address, amount, reason)` which submits a chain transfer from the omnibus back to the Darwin's wallet and records a `REFUND_TO_DARWIN` entry; the supervisor then credits the Darwin's local ledger via `wallet.credit` so the genome's `balance_usdc` reflects the inbound. Operator-initiated (not automatic on leg-2 failure) so a real ramp outage doesn't silently mask itself.

### Freelancer registry

Each owner registers their fiat destinations once. The platform exposes:

- `POST  /platform/freelancers` — create or replace. Body: `owner_subject`, `freelancer_id`, `fiat_destination` (Stripe Connect account id, MoonPay payout token, etc.), `display_name`, `kyc_status`, optional `metadata`.
- `DELETE /platform/freelancers/{owner_subject}/{freelancer_id}` — revoke (idempotent).
- `GET    /platform/freelancers/{owner_subject}` — list entries for one owner.

Routes are platform-operator scoped today; owners onboard via the operator. A self-service `/me/freelancers` path scoped to the authenticated owner is a future addition. Entries persist through the `freelancer_registry` SQL table (schema v5); a process restart preserves every owner's already-onboarded freelancers.

**3. x402 / web service that handles fiat conversion downstream.** The Darwin pays USDC via the standard `x402` client; the upstream merchant (a content network, an AI API, an ad platform) settles in fiat downstream on their own books. The Darwin never sees fiat. This is the dominant path the system is designed for — most agent-economy services accept USDC at the merchant edge today.

We do **not** support Darwins paying each other directly (cross-Darwin payments). Information and money should not flow between Darwins; that's a deliberate isolation property. If a future business model requires it, we'd build it as an explicit, platform-mediated, owner-consented feature.

## Reproduction transfers — parent → omnibus → child

When a Darwin reproduces, the parent's surplus needs to land in the new child's wallet. Doing this as a direct parent → child chain transfer requires the parent's wallet to know the child's address at the moment of reproduction; the child wallet might not exist yet. The platform mediates instead, identical to the fiat payout pattern:

   - **Leg 1** — the genome's `Spawner.spawn` calls `parent.wallet.transfer_out_for_child(budget, destination=omnibus.address())`. The parent's USDC ends up at the omnibus address on-chain.
   - **Leg 2** — the supervisor's `_observe_children` loop boots the child (giving it its own wallet and address), then calls `omnibus.relay_reproduction(parent_id, child_id, child_address, amount)`. The omnibus submits the chain transfer from itself to the child's per-Darwin address and records a `RELAY_REPRODUCTION` ledger entry. `user_credits` is untouched — the parent's wallet, not a user deposit, is the source.

A chain hiccup on leg 2 is audit-logged via `reproduction_relay_failed` and surfaced for operator reconciliation, but never crashes the spawn-observation loop: the child is alive even if the omnibus-side settlement needs a retry.
