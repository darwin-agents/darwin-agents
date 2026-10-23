# Darwin — Documentation

**A SaaS platform for launching evolutionary, economically-autonomous AI agents.**

A user signs up, deposits a starting budget, and launches a *Darwin* — a digital organism with an immutable DNA core that spawns specialized worker neurons, explores online business models, and tries to stay alive by earning more than it spends. If it runs out of capital it dies. If it turns a profit it reproduces, encoding the lessons of its life into the next generation's DNA.

> **Status: testnet alpha (v0.0.6 + unreleased, 2026-10).** The genome (`darwin_dna`), the platform control plane (`darwin_platform`), the Next.js dashboard with its PWA escalation companion, and the Hetzner k3s infrastructure are built and tested. What remains before real money moves is operational, not code: the gates in [`../engineering/32-pre-mainnet-gates.md`](../engineering/32-pre-mainnet-gates.md) and the checklist in [`../engineering/30-mainnet-cutover.md`](../engineering/30-mainnet-cutover.md). The design docs below predate the build; where the implementation diverged they carry **As built** notes.

---

## How to read these docs

Read `overview/` first for the idea, then `engineering/` for how it is actually built. The hub for all folders is [`../README.md`](../README.md).

| Folder | Contents |
|---|---|
| [`overview/`](.) | The global idea and the docs that aren't strictly the deployable core: the pitch, vision & metaphor, design principles, prior art, market landscape, legal/ToS, the phased roadmap, and platform deployment & infrastructure. |
| [`engineering/`](../engineering/) | The technical design of the core Darwin agent: system architecture, DNA core, soma & orchestrator, neurons, treasury & payments, evolution & reproduction, isolation & multi-tenancy, the platform control plane, kill-switch and anti-wireheading mechanics, the full tech stack, the Layer 0 build plan, and system diagrams. |
| [`frontend/`](../frontend/) | The Next.js dashboard and its installable PWA escalation companion (Web Push): screens, Anatomy/Timeline canvases, API integration, auth, and phased delivery. |
| [`marketing/`](../marketing/) | Non-technical message source for the landing page and public material. |
| [`postmortems/`](../postmortems/) | Filled-in incident postmortems (template: engineering doc 29). |

### `overview/`

- [`00-README.md`](00-README.md) — this file
- [`01-vision-and-metaphor.md`](01-vision-and-metaphor.md) — what Darwin is, the biological metaphor, why an organism rather than just an agent, what it is not, the product
- [`02-design-principles.md`](02-design-principles.md) — the ten principles (P1–P10) every design decision is checked against
- [`03-prior-art.md`](03-prior-art.md) — DGM, Voyager, agent frameworks, agentic payments, sandboxes, prior art and what's genuinely new
- [`04-market-landscape.md`](04-market-landscape.md) — realistic revenue archetypes a Darwin can pursue
- [`05-legal-and-tos.md`](05-legal-and-tos.md) — legal entity, regulatory regimes, ToS checklist, liability surface
- [`06-roadmap-and-phases.md`](06-roadmap-and-phases.md) — the four-phase build plan with definitions of done and kill criteria
- [`07-deployment-and-infrastructure.md`](07-deployment-and-infrastructure.md) — phase-by-phase infrastructure, Kubernetes shape, Terraform layout, DR

### `engineering/`

See [`engineering/00-README.md`](../engineering/00-README.md) for the full index.

## The one-paragraph pitch

Darwin is a multi-tenant SaaS where each user spawns an autonomous agent with a fixed starting budget. The agent has an immutable **DNA core** (rules, ethics, evolution machinery, kill-switches) and a mutable **soma** (the code it writes for itself). It orchestrates a swarm of specialized **neurons** — worker sub-agents that handle one job each: content, accounts, ads, browser automation, trading, support. The agent earns money through whatever its neurons can run — online ventures, trading, market positions — pays its own infrastructure bills out of a programmable treasury, and escalates to its human owner only when blocked by a captcha, KYC, or another physical-world bottleneck. When the agent crosses a profitability threshold it spawns a child: a new Darwin whose DNA is the parent's DNA plus a new immutable layer encoding what worked. When it runs out of money, it dies.

## Why now

Three things converged in 2025–2026 to make this buildable:

1. **Self-modifying agents work.** The Darwin Gödel Machine (Sakana AI, 2025) showed a coding agent can rewrite its own code and verifiably improve on benchmarks. The architecture pattern is proven; we are applying it to economic survival rather than benchmark performance.
2. **Agents can hold and spend money.** Coinbase Agentic Wallets and the x402 protocol (AWS Bedrock AgentCore Payments was evaluated and descoped — engineering doc 17) let an autonomous agent pay for APIs, compute, and services in stablecoins with programmable spending limits — no human in the loop at the transaction level.
3. **The agent toolchain is production-grade.** LangGraph for stateful orchestration, E2B/Modal for sandboxed code execution, Browser-Use + Stagehand on Browserbase for browser neurons, a self-hosted LiteLLM gateway for multi-provider LLM routing across text, image, video, and audio. The pieces exist; the work is composing them.

See [`03-prior-art.md`](03-prior-art.md) for sources.

## Design questions

Answered by the implementation (details in [`../engineering/06-evolution-and-reproduction.md`](../engineering/06-evolution-and-reproduction.md)):

- **Fitness function.** Profit weighted by counter-pressures, not pure profit: `fitness_profit_weight` 1.0, diversity bonus 0.1, escalation quality 0.05, policy-violation penalty 10.0 — fixed in the DNA (`darwin_dna/hyperparameters.py`, `machinery/fitness.py`).
- **Cross-Darwin learning.** Sibling lessons can be shared through the platform's cross-pollination archive (`darwin_platform/tenancy/cross_pollination.py`), screened against poisoning (`tests/redteam/test_cross_pollination_poison.py`). It is a supervisor option, not yet a CLI flag.

Still open, tracked as living issues:

- **Reproduction rules.** Children inherit the parent's sealed layer stack plus one new layer (with compaction); how much more should be sealed against mutation is still a tuning question (engineering doc 34).
- **Death conditions beyond bankruptcy.** Owner and operator kills, and quarantine for policy violations (engineering doc 09), exist; timeouts and repeated-violation death do not.
