# System overview

Darwin has two levels: the **platform** (one instance, multi-tenant SaaS) and the **organism** (one per user-launched Darwin). They share nothing at runtime — each Darwin is isolated — but the platform provisions, supervises, and bills them all.

## High-level picture

```
┌──────────────────────────── PLATFORM (multi-tenant SaaS) ────────────────────────────┐
│                                                                                      │
│   ┌───────────┐   ┌──────────────┐   ┌────────────┐   ┌──────────────────────────┐   │
│   │  Web app  │   │  Identity &  │   │  Billing & │   │ Owner notification bus   │   │
│   │ (Next.js) │   │  KYC (Auth)  │   │  Treasury  │   │ (push / email / webhook) │   │
│   └─────┬─────┘   └──────┬───────┘   │   ledger   │   └────────────┬─────────────┘   │
│         │                │           └─────┬──────┘                │                 │
│         │                ▼                  ▼                      │                 │
│         │        ┌────────────────────────────────────────┐        │                 │
│         └──────► │  Control plane (Darwin Supervisor)     │ ◄──────┘                 │
│                  │  - spawn / kill / pause / resume       │                          │
│                  │  - kill-switch enforcement             │                          │
│                  │  - resource quotas, anomaly detection  │                          │
│                  └────────────────────┬───────────────────┘                          │
│                                       │                                              │
└───────────────────────────────────────┼──────────────────────────────────────────────┘
                                        │ provisions / supervises N
                                        ▼
   ┌─────────────────────── ONE DARWIN ORGANISM (per user, isolated) ───────────────────┐
   │                                                                                    │
   │   ┌──────────────────── DNA CORE (immutable, signed) ─────────────────────────┐    │
   │   │  rules · ethics · evolution loop · kill-switch · treasury policy · audit  │    │
   │   │  Layer N   ← sealed lessons from generation N                             │    │
   │   │  Layer N-1 ← sealed lessons from generation N-1                           │    │
   │   │  ...                                                                      │    │
   │   │  Layer 0   ← founding genome (this is the first Darwin you ship)          │    │
   │   └────────────────────────────────┬──────────────────────────────────────────┘    │
   │                                    │ read-only                                     │
   │                                    ▼                                               │
   │   ┌──────────────── SOMA (mutable agent code, sandboxed) ─────────────────────┐    │
   │   │   Orchestrator (LangGraph) — plans, delegates, monitors                   │    │
   │   │       │                                                                   │    │
   │   │       ├──► Strategy engine (which business, which neurons, when)          │    │
   │   │       ├──► Self-modification engine (proposes & tests soma rewrites)      │    │
   │   │       └──► Escalation manager (talks to the owner via the platform)       │    │
   │   └────────────────────────────────┬──────────────────────────────────────────┘    │
   │                                    │ dispatches tasks                              │
   │                                    ▼                                               │
   │   ┌──────────────── NEURONS (specialized workers, sandboxed) ─────────────────┐    │
   │   │  content │ accounts │ browser │ ads │ trading │ support │ analytics       │    │
   │   │  ...each one a replaceable module with a stable contract...               │    │
   │   └────────────────────────────────┬──────────────────────────────────────────┘    │
   │                                    │                                               │
   │   ┌────────────────────────────────▼──────────────────────────────────────────┐    │
   │   │  TREASURY (per-Darwin USDC wallet, programmable spend rules via x402)     │    │
   │   │  + SHARED VAULT (KYC docs + account creds + API keys + DB credentials —   │    │
   │   │    bidirectional: user and Darwin both write; platform UI has RO view)    │    │
   │   │  + DARWIN-OWNED DATABASES (provisioned on demand, credentials in vault,   │    │
   │   │    platform UI reads them to render the Darwin's data in the dashboard)   │    │
   │   │  + EVENT LOG (append-only, hash-chained audit trail)                      │    │
   │   └───────────────────────────────────────────────────────────────────────────┘    │
   │                                                                                    │
   └────────────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼ outbound only, via gateways
                                  ┌────────────────────────┐
                                  │  External world        │
                                  │  - LLM / image / video │ ← via platform LiteLLM gateway (model-agnostic)
                                  │  - Payment rails       │ ← USDC/x402 (Coinbase CDP) + MoonPay/Stripe ramps
                                  │  - Web (browsers)      │ ← via Browser-Use on Browserbase
                                  │  - APIs (ads, blogs)   │ ← via per-neuron integrations
                                  └────────────────────────┘
```

For a richer rendering of this diagram see [`19-system-diagrams.md`](19-system-diagrams.md).

## The two layers, in plain terms

**The platform** is a normal multi-tenant SaaS: a web app, an auth provider, a Postgres database, a billing system, and a control plane that knows how to spawn, kill, and monitor Darwin organisms. It does **not** make any business decisions for the organisms it hosts; its job is provisioning, supervision, and enforcement of platform-wide rules (no terrorism, no CSAM, no sanctioned-list businesses, etc.).

**A Darwin organism** is what runs inside the platform on behalf of one user. Each organism has its own treasury, its own vault, its own event log, its own running orchestrator, its own neurons, and its own slice of compute. Organisms cannot talk to each other (this is enforced at the network and authorization layer — see [`07-isolation-and-multi-tenancy.md`](07-isolation-and-multi-tenancy.md)).

## Why this split

Three reasons:

1. **Blast radius.** A misbehaving Darwin can hurt its owner's treasury but not anyone else's. A misbehaving platform can hurt everyone, so the platform's surface area is kept small and boring.
2. **Selection.** Darwins must be isolated for evolutionary selection to mean anything. If two siblings can share lessons informally, fitness becomes a collective property and we lose the per-lineage signal we're trying to measure.
3. **Compliance.** Per-tenant isolation is what every payment processor, KYC provider, and data-protection regulator wants to see. We are not inventing a new compliance model; we are reusing the well-understood multi-tenant SaaS one.

## Where the docs go next

The remaining files drill into each box of the diagram:

- [`02-dna-core.md`](02-dna-core.md) — what's in the DNA, how it's loaded and verified, what the layer structure looks like
- [`03-soma-and-orchestrator.md`](03-soma-and-orchestrator.md) — the mutable agent code, the LangGraph orchestrator, the self-modification loop
- [`04-neurons.md`](04-neurons.md) — the neuron contract, the catalog of initial neurons, how new ones are added
- [`05-treasury-and-payments.md`](05-treasury-and-payments.md) — wallets, x402, fiat ramps, spend policy, ledger (AgentCore Payments is descoped; see doc 17)
- [`06-evolution-and-reproduction.md`](06-evolution-and-reproduction.md) — fitness, mutation, selection, the reproduction event
- [`07-isolation-and-multi-tenancy.md`](07-isolation-and-multi-tenancy.md) — sandboxing, networking, vault, per-tenant data isolation
- [`08-platform-control-plane.md`](08-platform-control-plane.md) — the SaaS side: supervisor, billing, dashboards, escalation routing
