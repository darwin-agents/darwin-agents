# Prior art and academic references

Darwin is not the first attempt at self-improving or economically-autonomous AI. This page indexes what we're building on.

## Self-improving agents

### Darwin Gödel Machine (Sakana AI + UBC + Vector, 2025)

The closest academic precedent. A self-improving coding agent that rewrites its own Python codebase, evaluates each variant on coding benchmarks, and maintains an open-ended archive of agent variants.

- **Paper:** "Darwin Gödel Machine: Open-Ended Evolution of Self-Improving Agents", arXiv:2505.22954, Jenny Zhang et al.
- **Results:** SWE-bench 20.0% → 50.0%; Polyglot 14.2% → 30.7%. Improvements compound over 80+ generations.
- **What we take from it:** the propose→stage→evaluate→archive→sample loop is exactly the self-modification engine pattern in our soma. The "empirical validation instead of formal proof" insight is core to our P5.
- **What we add to it:** real economic selection pressure (treasury can hit zero), specialized neurons rather than a monolithic agent, multi-tenancy, a custodial-grade payment integration, human-in-the-loop escalation as a primitive.
- **What we don't take:** DGM didn't address the cost of failed mutations (its sandbox was infinite); we have to. DGM didn't have a death condition; we do.
- **Reference implementations:** `jennyzzt/dgm`, `lemoz/darwin-godel-machine` on GitHub.

### The Gödel Machine (Schmidhuber, 2003)

The theoretical ancestor. A self-improving program that modifies itself only when it can *prove* the modification is beneficial. Beautiful but practically intractable on real-world code, where tractable proofs are rare. DGM and Darwin both choose empirical validation instead.

### Voyager (NVIDIA, 2023)

A Minecraft-playing agent that builds and reuses a library of skills (small code snippets) as it explores. Showed the value of atomic, reusable skill acquisition vs. monolithic policy updates. We mirror this in our neuron catalog and lesson-distillation pass.

### AlphaEvolve (DeepMind, 2024) / FunSearch (DeepMind, 2024)

Coding agents using evolutionary search over LLM-proposed programs. Validated that LLM-proposed code variants + evaluation + selection produces useful novelty. Different domain (algorithm discovery, math) but the same evolutionary loop pattern.

## Agent frameworks and architecture

### LangGraph (LangChain, 2024–)

The framework we use. See [`../engineering/12-orchestration-langgraph.md`](../engineering/12-orchestration-langgraph.md) for our specific application. Worth reading: the LangGraph "human-in-the-loop" docs and the "time-travel" feature for understanding the durability primitives Darwin depends on.

### CrewAI / AutoGen / Microsoft Agent Framework

Considered, not selected. Useful reading for understanding the design space; see Alice Labs' 2026 production-tested ranking and the various LangGraph-vs-CrewAI comparisons for the trade-offs.

### Claude Agent SDK

The Claude Agent SDK introduced patterns (hooks, skills, sub-agents) that we partially mirror in our neuron contract.

## Agentic payments

### x402 protocol (Coinbase, 2025)

HTTP-native machine-to-machine payments using the HTTP 402 status code and USDC stablecoins. The foundational protocol that makes Darwin's pay-its-own-bills story actually work.

- x402 V1 launched May 2025; V2 (Dec 2025) added reusable sessions, multi-chain support, automatic service discovery.
- By April 2026, ~69,000 active AI agents on x402 had processed >165M transactions and >$50M in volume.
- x402 Foundation incubated under the Linux Foundation, with Cloudflare, Stripe, AWS, Google, Visa, Circle, Solana Foundation among the backers.

### AWS Bedrock AgentCore Payments (AWS + Coinbase + Stripe, May 2026)

Amazon's developer SDK for adding x402-based payments to AI agents with explicit user authorization and per-session spending limits. Our secondary integration path.

### Coinbase Agentic Wallets (Coinbase, Feb 2026)

The wallet substrate we use per Darwin. Non-custodial, TEE-secured, built specifically for autonomous agents with programmable spend limits and gasless Base transactions.

### Competing protocols

- **AP2** (Google) — Agent Payments Protocol; standardizes authorization. Complementary to x402 (which is the settlement layer).
- **ACP** (OpenAI + Stripe) — Agent Commerce Protocol; standardizes checkout flows. Pivoted to app-based model.
- **MPP** (Stripe + Tempo, March 2026) — Machine Payments Protocol; agent sessions with streaming micropayments.

We use x402 as the primary settlement protocol; if AP2's authorization model matures, we adopt it on top. The choice isn't permanent — wallet providers and gateways increasingly support multiple protocols simultaneously.

### Stablecoins as agentic money

Total stablecoin transaction volume in 2025 was ~$33T (per industry analyst figures). USDC supply grew ~56% in 2026 to ~$420B, with agentic and machine-to-machine payments named as a growth driver. This isn't speculative; it's real economic activity happening now.

## Browser automation for agents

### Browser-Use (open-source, 2024–)

Our **primary** browser framework. 50K+ GitHub stars as of 2026, Python-native, wraps the browser with an autonomous LLM-driven decision loop ("give it a goal, it figures out the steps"). The autonomous model fits Darwin's exploratory workload — a young Darwin is constantly poking at unfamiliar sites and signing up for new services. See [`../engineering/15-browser-stack.md`](../engineering/15-browser-stack.md).

### Stagehand (Browserbase, 2025)

Our **caching/optimization layer**, not the primary driver. Stagehand's action caching with self-healing replay turns a mastered workflow into near-zero-LLM-cost deterministic replay. Darwin discovers a workflow exploratively with Browser-Use, then promotes stable repeated workflows to a cached Stagehand (or `workflow-use`) replay. Browser-Use is the primary driver (Python-native, autonomous-first); Stagehand's role is the repeat-cost optimizer.

### Browserbase

The managed cloud-browser runtime both frameworks run against — Chromium fleet, stealth profiles, residential proxies, session recording, live takeover (how owners resolve captcha escalations).

### Vision-driven computer control (Anthropic Computer Use, OpenAI CUA)

Generic desktop/OS-control products: full-screen screenshots plus synthetic mouse/keyboard over an entire machine. Darwin does **not** use them — it only ever drives a remote browser on Browserbase, so whole-computer control is the wrong abstraction. Benchmarks also show vision-driven control is 12–17 percentage points less reliable than DOM-driven navigation, which is why Darwin is DOM-first. For the rare page the DOM genuinely can't express, Darwin falls back to Browser-Use's own vision mode on the same Browserbase session (capped), then escalates to a human live-takeover. See [`../engineering/15-browser-stack.md`](../engineering/15-browser-stack.md).

## Code execution sandboxes

### E2B (2024–)

The primary sandbox provider; Firecracker microVMs, Python/Node/arbitrary binaries, sub-second cold starts. Specifically designed for untrusted LLM-generated code.

### Modal, Daytona, Northflank, Blaxel

Considered alternatives with different trade-offs (GPU support, BYOC, persistence). See [`../engineering/14-sandboxing-e2b.md`](../engineering/14-sandboxing-e2b.md).

## Multi-tenant SaaS infrastructure

### HashiCorp Vault's namespace + AI agent identity model

Our pattern for per-tenant secret isolation. Vault's native AI agent identity support entered beta in mid-2026; we adopt it as it stabilizes.

### EKS multi-tenant patterns

AWS's reference architecture for namespace-per-tenant with dynamic credential vending. We follow the standard pattern; nothing novel here, intentionally.

## What's genuinely new in Darwin

To be precise about our contribution vs. our composition:

- **The treasury-as-fitness model.** DGM proved code self-improvement under benchmark selection. We are testing whether the same dynamic produces useful behavior under economic selection — agents that have to pay their own bills. This is novel and untested at scale.
- **Stacked, sealed DNA layers as the inheritance unit.** A specific design for what reproduction means in software, distinct from copying weights, copying code, or copying lessons-as-text. We don't claim it's optimal; we claim it's defensible and auditable.
- **The neuron / soma / DNA decomposition** mapped to a multi-tenant SaaS platform with operator-grade controls. Each piece is well-understood; the composition is the work.
- **Owner-as-supernatural-helper escalation as a control-flow primitive.** Most agent systems treat human intervention as exception handling. We treat it as a planned, designed-for state.

None of this is research-grade novelty. It is engineering integration of well-formed pieces into a system whose emergent dynamics nobody has actually run yet.
