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

## Coinbase Agentic Wallets

Each Darwin gets its own non-custodial Coinbase Agentic Wallet. Properties:

- **Per-wallet spending limits** enforced at the wallet level (in addition to our policy engine — defense in depth).
- **Gasless transactions** on Base; no need to keep a gas reserve.
- **Skills library** (`Authenticate`, `Fund`, `Send`, `Trade`, `Earn`) accessible via SDK.
- **TEE-secured key material;** Coinbase itself cannot drain a Darwin's wallet, and neither can we.
- **CDP Portal integration** for telemetry and operator-side monitoring.

The platform's role is to provision a wallet at Darwin spawn, configure its spend policy, and broker user funding/withdrawal at the omnibus edge.

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
- **MoonPay** as a secondary provider, especially for users who prefer to deposit crypto directly (BTC, ETH → swap to USDC at the edge). Their February 2026 Agents product is built for our exact use case.
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
- **Tax reporting.** We provide year-end statements (US 1099, UK SA, EU equivalents) summarizing revenue, costs, and harvest events per Darwin and aggregated per user. Tax-residency-aware.
- **Audit trail.** Every payment links back to the originating event log entry; the chain is hash-anchored daily.

## What about a Darwin paying a human?

Some strategies involve hiring humans — pay a freelance writer, pay a translator, tip a community moderator. The platform supports this via:

- **Direct USDC** to a verified wallet (low friction, low compliance overhead).
- **Stripe Connect** to a verified bank for fiat (higher friction, higher compliance overhead).

Both classes escalate to the owner by default — paying a human is a category our DNA constitution flags. The owner can pre-approve "Upwork escrow payments under $50" if they want lower-friction operations.

We do **not** support Darwins paying each other directly (cross-Darwin payments). Information and money should not flow between Darwins; that's a deliberate isolation property. If a future business model requires it, we'd build it as an explicit, platform-mediated, owner-consented feature.
