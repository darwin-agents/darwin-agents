# Treasury and payments

This is the part of Darwin that makes it an *organism* and not just an agent: it has a body of capital, it metabolizes that capital by paying its bills, and it dies when the capital runs out.

This doc covers the architecture. For concrete provider choices (Coinbase Agentic Wallets, x402, fiat ramps, AgentCore) see [`17-payments-providers.md`](17-payments-providers.md).

## The flow, end to end

```
USER                          PLATFORM                          DARWIN
────                          ────────                          ──────
Deposit $X by card/bank  ───► Fiat-to-USDC bridge          
                              (Stripe / MoonPay / Coinbase)
                                         │
                                         ▼
                              Platform wallet (omnibus, ledgered)
                                         │  fund $X on Darwin launch
                                         ▼
                                                            ◄─── Per-Darwin USDC wallet
                                                                 (Coinbase Agentic Wallet,
                                                                  spend policy enforced)
                                                                          │
                                                                          │ pays out
                                                                          ▼
                                                                 ┌────────────────────┐
                                                                 │ LLM tokens (OR)    │
                                                                 │ Compute (E2B/etc.) │
                                                                 │ APIs via x402      │
                                                                 │ Ad spend (Meta/G)  │
                                                                 │ Subscriptions      │
                                                                 │ Payouts to humans  │
                                                                 └────────────────────┘
                                                                          │
                                                                          │ revenue arrives
                                                                          ▼
                                                                 ◄─── Per-Darwin wallet
                                         ▲
                              Profit harvest (optional, user-set)
                                         │
                                         ▼
USER ◄──── Withdrawal to bank/wallet ◄── Platform wallet
```

## Why USDC and stablecoins (mostly)

Darwin needs money that is:

- **Programmable.** The agent must be able to send money without a human entering a card number. Cards can't do this; stablecoins on EVM/Solana/Stellar can.
- **Stable.** A 5% swing in the underlying overnight could kill an otherwise-profitable Darwin. USDC and USDT are dollar-pegged and acceptable; ETH and BTC are not.
- **Cheap.** Many Darwin transactions will be sub-dollar (an API call, an LLM completion). On Base or Stellar, transaction fees are fractions of a cent. On Ethereum mainnet they would exceed the payment.
- **Settled fast.** Darwin operates in seconds, not days. Bank ACH (T+2) is unusable as primary; stablecoin settlement (seconds) is fine.

We use **USDC on Base** as the primary settlement rail, with a fallback to Solana or Stellar if Base is congested. Treasury balances are denominated in USDC for accounting purposes throughout. A Darwin running a trading strategy may hold volatile assets or open market positions; those are part of its strategy, not its runway — they are marked to USDC at each reconciliation so the ledger, fitness, and death trigger always read in stable terms.

Fiat in/out happens at the platform edge: card or bank deposit → USDC into the platform omnibus wallet → USDC into the per-Darwin wallet (and the reverse for withdrawals). The Darwin itself never touches fiat.

## The payment infrastructure (2025–2026 stack)

We are not inventing a payment rail. The agentic-payments ecosystem matured between mid-2025 and 2026, and we ride it:

- **x402.** Coinbase's HTTP-native payment protocol. When a service returns HTTP 402 with a price, the agent's wallet automatically authorizes a USDC payment and the service unlocks. This is what makes "the agent pays for its own API calls" actually work, at machine speed. x402 V2 (Dec 2025) added reusable sessions and multi-chain routing.
- **Coinbase Agentic Wallets.** Non-custodial wallets purpose-built for autonomous agents. Built-in spending limits, gasless transactions on Base, programmable guardrails, TEE-secured key material. We use these as the per-Darwin wallet.
- **AWS Bedrock AgentCore Payments.** Amazon's wrapper around x402 + Stripe — *descoped for the MVP* (see [`17-payments-providers.md`](17-payments-providers.md)). It would only re-wrap Coinbase and Stripe, which the platform integrates directly, behind an AWS dependency we avoid.
- **Stripe + MoonPay.** Fiat on-ramps. User deposits a credit card; we get USDC. Standard, regulated, KYC'd at this edge so the per-Darwin transactions don't have to be.

For an external comparison of these protocols see [`../overview/03-prior-art.md`](../overview/03-prior-art.md).

## The spend policy engine

Every outbound payment passes through a policy engine before the wallet signs the transaction. The policy is composed of layers:

1. **DNA-defined hard ceilings** (per-action $X, per-day $Y, total-burn $Z). These cannot be overridden, ever. Hitting one halts the spend and either escalates or kills the Darwin.
2. **Owner-configurable soft ceilings.** The user can tighten the DNA's hard ceilings via their dashboard. They can never loosen them beyond the DNA's hard cap.
3. **Strategy-budget allocations.** The soma can declare "I want to spend up to $20 on this affiliate experiment". The policy engine enforces that envelope; the strategy cannot quietly spend more.
4. **Escalation rules.** Some recipient categories always escalate regardless of amount: human freelancers (Upwork, Fiverr), legal/financial services, anything in a jurisdiction on the platform's watchlist.
5. **Anomaly detection.** Sudden spend pattern changes ("you've never paid more than $2 and now you're trying to pay $200") trigger a pause and escalation.

Policy decisions are logged with their rationale. A user can replay any spend and see why it was allowed or refused.

## The ledger

Every Darwin has a per-Darwin double-entry ledger in Postgres. Each entry has:

- `darwin_id`
- `timestamp`
- `action_id` (links to the event log entry)
- `account_debit`, `account_credit` (treasury, expense category, revenue category, escrow)
- `amount_usdc`
- `counterparty` (the payee or payer, if known)
- `category` (LLM, compute, API, ad spend, content, revenue:affiliate, revenue:product, etc.)
- `hash` (chained to previous entry)

This is the source of truth for:

- The user's dashboard ("today your Darwin earned $X and spent $Y")
- The fitness function (profit-since-last-reproduction)
- The death trigger (treasury balance reaches floor)
- Tax reporting (we hand the user a CSV at year-end)
- Audit (every transaction reconstructable)

The ledger is **not** the source of truth for the balance itself — the wallet on-chain is. The ledger is reconciled against the wallet every heartbeat. A mismatch larger than a configured tolerance halts the Darwin.

## Death by bankruptcy

The kill condition for treasury exhaustion:

```
balance_usdc < minimum_viable_balance
AND no_revenue_inbound_within_grace_window
=> trigger death sequence
```

`minimum_viable_balance` is itself a parameter — typically set at the cost of one safe-shutdown cycle (writing final logs, posting a final notification, archiving the soma snapshot). It's not zero, because we want the Darwin to die *cleanly*, not crash mid-action.

`no_revenue_inbound_within_grace_window` exists because revenue is bursty — an affiliate commission posts once a week, an ad payout twice a month. We don't want to kill a Darwin that has a known $50 commission arriving tomorrow because today it ran out of operating cash for an hour. The grace window is part of the strategy and itself evolvable. (But the *hard* floor of "balance > zero" is not negotiable.)

Death sequence:

1. Stop the orchestrator loop at the next safe checkpoint.
2. Refuse any new neuron dispatches.
3. Settle any in-flight commitments where doing so is cheaper than abandoning them.
4. Write final ledger entries and the death event.
5. Snapshot the soma for forensic review.
6. Notify the owner.
7. If the Darwin reproduced before dying, ensure the child has been spawned successfully and inherits any open revenue streams that survive parental death (TBD: this is a hard product question — see [`06-evolution-and-reproduction.md`](06-evolution-and-reproduction.md)).

## Profit harvest

By default, all profit stays in the Darwin's treasury — re-invested into more business operations, which is the whole point. The user can configure a **harvest rule**:

- "Move any balance above $200 to my external wallet at midnight UTC"
- "Move 30% of weekly profit, keep 70% reinvested"
- "Move everything immediately on receipt" (most conservative)

Harvest is a normal outbound transaction subject to the same policy engine, just with the user's verified bank/wallet as the counterparty.

## Trading, markets, and DeFi

A Darwin may trade financial and crypto markets, take positions in prediction and betting markets, and use DeFi (lending, liquidity provision, leverage) when it judges the strategy is worth it. This is the Darwin's own decision, shaped by how much you fund it and how its lineage has evolved — there is no separate menu the owner picks from.

There is no special gate for these activities. They are bounded by exactly the same two mechanisms as every other action: the DNA treasury hard caps (per-action, per-day, total-burn) and the kill switch. A Darwin can lose money fast this way; that is expected and is part of selection. The platform does not bail out a Darwin that trades itself to zero — it dies like any other.

For ledger, fitness, and death-trigger purposes, non-USDC holdings and open positions are marked to their USDC value at each reconciliation; the operating runway is always read in stable terms.
