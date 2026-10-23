# Darwin — Engineering documentation

The technical design of the core Darwin agent and the platform that hosts it. Read [`../overview/`](../overview/) first for the idea and the rationale; this folder is the *how*.

The docs are numbered and meant to be read roughly in order, but each stands alone. Design docs (01–17) record the design and its rationale, with **As built** notes where the implementation diverged; build plans (18, 20–22) are historical plans with status banners; the reference docs (23–30) describe the system as it runs today. The hub for every docs folder is [`../README.md`](../README.md).

## Architecture (the organism + the platform)

- [`01-system-overview.md`](01-system-overview.md) — the two levels (platform vs. organism), the high-level picture, why the split
- [`02-dna-core.md`](02-dna-core.md) — what's in the immutable DNA, the stacked-layer model, signing & verification
- [`03-soma-and-orchestrator.md`](03-soma-and-orchestrator.md) — the mutable body, the LangGraph main loop, the self-modification engine
- [`04-neurons.md`](04-neurons.md) — the neuron contract, the initial catalog, routing, isolation, costing
- [`05-treasury-and-payments.md`](05-treasury-and-payments.md) — the flow end-to-end, the spend policy engine, the ledger, death by bankruptcy
- [`06-evolution-and-reproduction.md`](06-evolution-and-reproduction.md) — fitness, the reproduction event, the archive, generation depth
- [`07-isolation-and-multi-tenancy.md`](07-isolation-and-multi-tenancy.md) — the four isolation boundaries, the shared vault, per-tenant data
- [`08-platform-control-plane.md`](08-platform-control-plane.md) — the supervisor, escalation router, dashboards, billing
- [`../frontend/`](../frontend/) — Next.js dashboard and mobile client (screens, canvases, API wiring)
- [`09-kill-switches-and-owner-controls.md`](09-kill-switches-and-owner-controls.md) — the three kill triggers, the graded response, owner & operator controls
- [`10-alignment-and-anti-wireheading.md`](10-alignment-and-anti-wireheading.md) — how the architecture resists pathological optimization and deception

## Tech stack

- [`11-tech-stack-at-a-glance.md`](11-tech-stack-at-a-glance.md) — the whole stack on one page with rationale
- [`12-orchestration-langgraph.md`](12-orchestration-langgraph.md) — why LangGraph, how we use it, persistence layout
- [`13-llm-routing-litellm.md`](13-llm-routing-litellm.md) — the self-hosted LiteLLM gateway, capability profiles, failover
- [`14-sandboxing-e2b.md`](14-sandboxing-e2b.md) — E2B microVMs, why not containers, quotas
- [`15-browser-stack.md`](15-browser-stack.md) — Browser-Use primary, Stagehand cached replay, Browserbase runtime
- [`16-secrets-and-vault.md`](16-secrets-and-vault.md) — HashiCorp Vault, the bidirectional shared vault, KYC handling
- [`17-payments-providers.md`](17-payments-providers.md) — concrete provider choices: Coinbase Agentic Wallets, x402, fiat ramps

## Build plan & diagrams

- [`18-layer-zero-build-plan.md`](18-layer-zero-build-plan.md) — the concrete file-by-file plan for the founding genome
- [`19-system-diagrams.md`](19-system-diagrams.md) — ASCII reference renderings of the platform and the organism
- [`20-phase-zero-runtime-build-plan.md`](20-phase-zero-runtime-build-plan.md) — wiring Layer 0 into the supervisor, adapters, persistence, and the 24h soak test
- [`21-phase-one-evolution-build-plan.md`](21-phase-one-evolution-build-plan.md) — self-modification staging, reproduction, real economic activity, and the evolution loop
- [`22-phase-two-multi-tenant-build-plan.md`](22-phase-two-multi-tenant-build-plan.md) — tenant isolation, full payment stack, user dashboard, and operational maturity for alpha

## Reference & hardening

- [`23-data-model-reference.md`](23-data-model-reference.md) — canonical Postgres schemas, state machine, checkpoint format, audit shape, migration strategy
- [`24-security-threat-model.md`](24-security-threat-model.md) — attack surfaces, adversarial soma contract, red-team checklist, kill-switch timing proof
- [`25-operational-runbook.md`](25-operational-runbook.md) — deployment, monitoring, failure modes, recovery, soak test operations
- [`26-testing-strategy.md`](26-testing-strategy.md) — the eight test tiers (unit → simulation, incl. infra, e2e, performance), adversarial posture, red-team mapping, CI map
- [`27-configuration-reference.md`](27-configuration-reference.md) — every CLI flag (generated from the CLI), env var, Helm value, and genome constant
- [`28-cross-cutting-contracts.md`](28-cross-cutting-contracts.md) — error handling philosophy, retry/halt/escalate rules, timeouts, idempotency
- [`29-incident-postmortem-template.md`](29-incident-postmortem-template.md) — structured format for documenting and learning from failures; filled-in incidents live in [`../postmortems/`](../postmortems/)
- [`30-mainnet-cutover.md`](30-mainnet-cutover.md) — testnet → real-money checklist: the `--network` switch, hard gates, credentials, go-live status
- [`31-browser-use-evaluation.md`](31-browser-use-evaluation.md) — research note re-evaluating Browser Use Cloud against the Browser-Use-on-Browserbase split (doc 15)
- [`32-pre-mainnet-gates.md`](32-pre-mainnet-gates.md) — the operator gates that must be *run* before real money moves: soaks, drills, real-cloud IaC verification, external reviews
- [`33-soma-activation-design.md`](33-soma-activation-design.md) — how promoted self-modifications actually load and run (`SomaLoader`, overlay, hot-swap)
- [`34-evolution-tuning-guide.md`](34-evolution-tuning-guide.md) — reading the archive, fitness trajectories, hyperparameter calibration, reproduction diagnostics (numbered 30 until 2026-10)
- [`BACKLOG.md`](BACKLOG.md) — doc-vs-implementation audit: what is done, partial, or missing, with evidence

## Repository tooling (repo root)

- [`../../Makefile`](../../Makefile) — `make help` lists install, the eight test tiers, lint, the Docker and kind stacks, `docs-config`, changelog sync, and semver release
- [`../../README.md`](../../README.md) — quickstart and contributing conventions (Conventional Commits, changelog workflow)
- [`../../tests/README.md`](../../tests/README.md) — one-page map of the test suite
- [`../../infra/README.md`](../../infra/README.md) — Docker, Helm, Terraform, and Grafana assets; [`../../infra/OPERATOR_RUNBOOK.md`](../../infra/OPERATOR_RUNBOOK.md) — provider matrix and failure-response checklist

## Related

- [`../marketing/`](../marketing/) — public-facing message source (non-technical by design)
- [`../overview/02-design-principles.md`](../overview/02-design-principles.md) — the P1–P10 principles every doc here is checked against
- [`../overview/06-roadmap-and-phases.md`](../overview/06-roadmap-and-phases.md) — the phased build plan
- [`../overview/07-deployment-and-infrastructure.md`](../overview/07-deployment-and-infrastructure.md) — infrastructure phase by phase
