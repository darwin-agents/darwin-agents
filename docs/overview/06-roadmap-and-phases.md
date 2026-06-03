# Roadmap and phases

The build is split into four phases. Each has a definition of done and a kill criterion. We do not advance to phase N+1 until phase N's success criteria are met, and we are willing to abandon the project if a phase's kill criterion fires.

This is opinionated. We have a finite runway and an unproven core hypothesis ("an agent with skin in the game evolves usefully"). We test the hypothesis early and stop spending if it doesn't survive contact with reality.

## Phase 0 — Foundation (target: weeks 0–8)

**Goal:** A single hand-built Darwin runs end-to-end in a sandbox, makes a few decisions, spends a few dollars, and writes a coherent ledger. No evolution yet. No multi-tenancy yet. We are proving the _plumbing_.

### What we build

**The Layer 0 founding genome (the most important code in the project):**

- DNA package skeleton with signature verification, layer loading, kill-switch handler, audit logger, treasury policy engine
- Founding orchestrator graph in LangGraph (perceive → plan → dispatch → reflect → check loops)
- Three neurons: `content.writer`, `browser.operator`, `payments.facilitator`
- Constitutional rules (allowed categories, forbidden actions, spend ceilings)
- A minimal fitness function (just profit-since-launch; no diversity or escalation-quality scoring yet)

See [`../engineering/18-layer-zero-build-plan.md`](../engineering/18-layer-zero-build-plan.md) for the concrete build plan of this piece.

**The infrastructure substrate:**

- A LiteLLM Proxy deployment in dev (single replica, Postgres-backed, a YAML model_list covering tier=fast/balanced/reasoning/coding/embed)
- An E2B project, one orchestrator sandbox and one neuron sandbox profile
- A single Postgres for both platform and Darwin data (we colocate in phase 0)
- A vault — OSS Vault running in Docker Compose for dev, no namespaces yet (paths-based)
- A treasury — Coinbase Agentic Wallet in testnet (Base Sepolia) with mock USDC

**A control plane skeleton:**

- A FastAPI app exposing the supervisor's `spawn / pause / kill / status` endpoints
- A trivial Next.js page that lists Darwins and shows the event log

**No real money flow yet** — payments are simulated via fake counterparties that return canned 402 responses.

### Definition of done (Phase 0)

- A Darwin spawns, runs its orchestrator loop for 24 hours straight without crashing
- It executes ≥ 50 neuron dispatches with ≥ 80% success rate
- Every action appears in the event log with a valid hash chain
- The ledger reconciles against the (mock) wallet balance with zero discrepancy
- A kill-switch flip halts the Darwin in < 5 seconds
- We can replay any moment of the Darwin's life from the event log

### Kill criterion (Phase 0)

- After 12 weeks, the Darwin still crashes more than once per 8 hours of runtime under our test load
- OR: the founding orchestrator costs more than $20/day in LLM tokens just to keep the loop alive (without doing useful work) — this means our cost model isn't viable

## Phase 1 — Evolution loop (target: weeks 8–16)

**Goal:** A Darwin can rewrite its own soma based on observed failures, and we have empirical evidence the rewrites either help or are correctly rejected. Reproduction works mechanically.

### What we build

**The self-modification engine:**

- The `propose_mutation` node — coding-tier LLM proposes patches against the soma
- The patch staging pipeline (fresh sandbox, copy soma, mount DNA read-only, apply patch)
- The evaluation harness (in DNA, frozen): runs the proposed soma against replayed historical situations
- The promotion logic — merge into live soma at a safe checkpoint
- The archive — Postgres tables holding both promoted and rejected variants with their fitness scores

**The reproduction event:**

- Lesson distillation pass (structured lessons, not arbitrary code; see [`../engineering/06-evolution-and-reproduction.md`](../engineering/06-evolution-and-reproduction.md))
- DNA layer signing service (platform-side, HSM-backed in prod; AWS KMS in this phase)
- Child Darwin spawn — new wallet, new vault namespace, new event log, fresh soma + N+1 DNA layers
- Lineage tree storage and basic visualization

**Real economic activity (small):**

- One realistic business archetype hard-coded in Layer 0's strategy priors: content + affiliate
- Real LLM costs metered against a real treasury (now mainnet USDC on Base, funded from our test pool)
- Real outputs: a content site running somewhere we control, affiliate links real, traffic synthetic

**Extended neuron catalog:**

- `content.publisher` — posts to WordPress
- `accounts.manager` — handles signup with mandatory escalation on captchas
- `analytics.measurer` — reads basic metrics
- `infrastructure.provisioner` — requests new infra from the platform
- `vault.writer` — centralized vault writes

### Definition of done (Phase 1)

- A Darwin self-modifies at least once, the modification passes evaluation, and post-promotion fitness measurably improves (or the modification is correctly rejected)
- A reproduction event completes: child Darwin spawns, inherits a new DNA layer, boots fresh soma, runs its own loop
- The lineage tree shows 2+ generations
- The Darwin earns at least one real dollar (or 1 USDC) of revenue
- The audit trail reconstructs all of the above

### Kill criterion (Phase 1)

- After 16 weeks, no successful self-modification has measurably improved fitness, and we cannot identify why (means either the harness is wrong or the loop isn't producing useful mutations — both fatal)
- OR: every Darwin we spawn dies of bankruptcy before earning anything, and we can't find a viable starting strategy in Layer 0

## Phase 2 — Multi-tenant alpha (target: weeks 16–28)

**Goal:** Real users can sign up, fund a Darwin, watch it run, and harvest profits. A handful of internal/friendly users, not a public launch.

### What we build

**Multi-tenancy hardening:**

- Vault Enterprise (or HCP Vault) with one namespace per Darwin
- Per-Darwin Postgres schemas with RLS; eventual move to per-Darwin DBs for organism data
- Per-Darwin Kubernetes namespaces with NetworkPolicies
- The egress proxy with audit logging and blocklist enforcement
- E2B in prod with proper sandbox lifecycle (rotation, replacement, quotas)

**The full payment stack:**

- Stripe for fiat deposits (cards, ACH, SEPA)
- KYC integration (Persona or Onfido — final pick by phase 2 kickoff)
- Real Coinbase Agentic Wallet provisioning per Darwin (mainnet)
- Per-Darwin x402 flows for paying the platform's LiteLLM, E2B, browser infra
- Sanctions screening (Chainalysis or TRM)
- Harvest rules — profit moves to user's verified wallet/bank on schedule

**The user-facing dashboard:**

- The **Anatomy** canvas — live organism visualization
- The **Timeline** canvas — chronological feed
- The Vault view — bidirectional, with provenance
- The Data browser — read-only window into the Darwin's databases
- Escalation inbox with mobile push notifications (Expo / React Native, mobile MVP)
- Lineage tree visualization

**The supervisor in earnest:**

- Resource quota enforcement
- Anomaly detection (spend velocity, behavior drift, heartbeat health)
- Graded response: log → throttle → pause → quarantine → kill
- Escalation routing with classification (captcha / KYC / approval / info)
- Platform-side billing reconciliation

**Operational maturity:**

- Honeycomb tracing across the platform and Darwin telemetry
- Audit log with daily Merkle anchoring to a public chain
- Runbooks for the top 10 incident types we can foresee
- On-call rotation, paging
- Backup and disaster recovery for Postgres, Vault, S3

### Definition of done (Phase 2)

- 5+ alpha users have each funded and run a Darwin for ≥ 2 weeks
- ≥ 50% of alpha-user Darwins survive their first 7 days (i.e. don't immediately die of bankruptcy)
- ≥ 1 alpha-user Darwin reaches generation 2 (successful reproduction by a paying user's Darwin)
- ≥ 1 escalation per alpha-user successfully resolved end-to-end via the mobile push flow
- No tenant-isolation incidents (verified via internal pen-test and a third-party audit before going further)
- Platform infra cost per active Darwin is < $40/month (otherwise the unit economics don't work)

### Kill criterion (Phase 2)

- Alpha users universally find the product confusing, useless, or distrustful
- Tenant-isolation incident occurs that we can't fix structurally
- Unit economics don't close: Darwin metabolism costs more than the platform's pass-through margin can cover, _and_ no obvious optimization path

## Phase 3 — Public beta and beyond (target: weeks 28+)

**Goal:** Open the platform to a public waitlist, then to general signup. Expand the neuron catalog. Begin experimentation on cross-lineage learning and other open architecture questions.

### What we build (in rough priority order)

**Public-launch readiness:**

- Pricing, terms of service, privacy policy
- Money transmitter posture (MTL, partnership, or non-custodial structuring — see [`05-legal-and-tos.md`](05-legal-and-tos.md))
- Customer support tooling and escalation workflows for human-to-human issues
- Marketing site, onboarding flows, fraud screening at signup

**Capability expansion:**

- `ads.manager` neuron with hard caps and mandatory owner pre-approval per campaign
- `support.responder` neuron for the user's own community/inbox
- Multimodal neurons: `content.image_creator`, `content.video_producer` (now that LiteLLM image/video gen is in play)
- More strategy archetypes in Layer 0 (or in a Layer 0.1 update for new Darwins)
- `trading.operator` neuron — live financial/crypto market trading and prediction/betting-market positions, bounded by the Darwin's treasury hard caps and the kill switch

**Architecture experiments:**

- Cross-lineage learning prototype — anonymous lesson contribution to a curated pool, opt-in per user
- Variation injection at reproduction (forced randomness magnitude calibration)
- Compaction pass for deep lineages (gen 20+)
- Owner-mediated mutation suggestions (do humans add value here, or do they degrade lineage fitness?)

**Scale infrastructure:**

- Split platform and organism Postgres into separate clusters
- Multi-region deployment for users outside the launch region
- Cost-driven LLM routing optimization (LiteLLM's cost-based routing + our per-strategy budgets)
- Sandbox-fleet autoscaling

**Phase-3 things we deliberately defer:**

- Sexual reproduction across Darwins
- Path C neuron generation (Darwins writing entirely new neuron classes)

### Success looks like (phase 3 — qualitative)

- Hundreds of active Darwins
- At least one publicly-known lineage that has survived for 3+ months and produced 5+ generations
- The platform's unit economics work — we are at break-even or trending toward it
- The "agent with skin in the game evolves usefully" hypothesis has been empirically supported by lineage telemetry
- We have learned things from operating the platform that surprise us

### Kill criterion (phase 3)

- 6 months in, no public-known successful lineage; mostly noise
- A regulator (in any of our launch jurisdictions) shuts us down or forces a structural change we can't afford
- A safety incident (financial loss to a user beyond their funded amount, or a Darwin doing something that harms third parties) that we cannot reasonably prevent recurring

## Cross-cutting concerns through all phases

Some work doesn't fit neatly in a phase — it runs continuously.

### Layer 0 evolution

Layer 0 is _the_ founding code. We will spend phases 0–2 refining it based on what running Darwins teach us. A Layer 0 update doesn't apply to existing Darwins (their DNA is immutable); it applies to _newly spawned_ Darwins. We version Layer 0 semantically (`L0-v1.0.0`, `L0-v1.1.0`, etc.) and treat each version as a deliberate release.

### Infrastructure-as-code

From day 1, all infrastructure is Terraform + Helm. We do not click in cloud consoles for anything production-related. The IaC repo lives alongside the platform code. (Pulumi was considered for TS-side resources but not adopted — a second IaC tool would be redundant; see `engineering/11-tech-stack-at-a-glance.md`.)

### Documentation

This doc tree is treated as a deliverable, not a journal. A change to architecture requires a docs PR. We expect new sections to appear (the operations runbook, the data model reference, the API reference) but everything stays in-repo.

### Eval harness for Darwin behavior

Throughout all phases, we maintain a simulated environment for testing Darwin behavior against scripted scenarios. This is _not_ the per-Darwin runtime fitness evaluator; it's our internal regression suite. New Layer 0 versions, new neurons, new constitutional rules all pass through the eval before any rollout.

### Cost governance

Phase 0 we measure cost per Darwin manually. Phase 1 we automate it. Phase 2 it's a first-class operational metric with alerting. Phase 3 we have predictive cost modeling per strategy archetype. At every phase, we know what a Darwin costs to run.

## What's deliberately not on this roadmap

- A mobile app beyond the escalation push-notification client in phase 2. The full mobile experience is a phase-4 problem.
- Internationalization. Launch in English, expand later.
- Enterprise / team accounts. One user, one wallet, N Darwins is the only shape we support.
- A marketplace where users can buy/sell strategies or neurons. Interesting; not now.
- An SDK for users to write their own custom neurons. Path C territory; not yet.
- Open-sourcing any of this. Maybe later; not at launch.
