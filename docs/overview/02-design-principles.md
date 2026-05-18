# Design principles

These are the principles every design decision must check itself against. They are intentionally short and bossy, because they're meant to settle arguments.

## P1 — DNA is immutable. Soma is not.

The DNA core is a signed, versioned Python package. The running Darwin can read it, but cannot write to it. Every self-modification happens in the soma — a sandboxed working directory containing the agent's generated code. The DNA contains the rules; the soma contains the behavior. Children inherit DNA + a new sealed layer; nothing in any ancestor's DNA can ever be changed.

**Operational consequence:** the DNA loader verifies a cryptographic signature on every boot. A tampered DNA refuses to launch.

## P2 — Skin in the game is the whole point.

A Darwin without a real budget that can hit zero is not a Darwin — it's a toy. Selection pressure requires that bad decisions be punished. The treasury must be real money (or stablecoins, programmatically equivalent), and the kill condition must be honored without grace periods or platform bailouts.

**Operational consequence:** the platform may not refund a dead Darwin. Users must be told this clearly at signup.

## P3 — The neuron is the unit of capability.

Capability lives in narrow, replaceable neurons — not in a monolithic god-agent. A "content neuron" knows how to write and publish. A "browser neuron" knows how to click. An "accounts neuron" knows how to sign up for things. The orchestrator (the soma) plans; the neurons execute. Adding a new capability means adding a new neuron, not rewriting the agent.

**Operational consequence:** neurons have a stable interface (input contract, output contract, cost, latency, failure modes) so the orchestrator can swap them.

## P4 — Escalate, don't fail.

When a Darwin hits something only a human can resolve — a captcha, a KYC document, a verification call, a spend approval above threshold — it does **not** fail the task. It pauses the neuron, posts an escalation to the owner's inbox, and resumes when the owner responds. Escalation is a first-class control-flow primitive, not an error path.

**Operational consequence:** every neuron must declare its escalation surface upfront (what classes of human help it can request) and the orchestrator must support indefinite pauses without losing state.

## P5 — Empirical fitness, not theoretical proof.

We do not try to formally prove a self-modification is beneficial (the Gödel Machine path). We evaluate empirically: did the modified agent earn more, or learn more, in the test environment? If yes, promote it. If no, archive it and try again. This is the DGM lineage's core insight.

**Operational consequence:** every proposed soma rewrite runs in a staging sandbox with simulated revenue/cost streams before it can touch the production Darwin.

## P6 — Multi-provider, no lock-in for the agent's brain.

The Darwin's LLM calls go through a platform-operated gateway (LiteLLM Proxy) so the agent can pick a model per task: cheap-and-fast for routine work, expensive-and-smart for hard reasoning, code-specialist for self-modification, plus image/video/audio for multimodal neurons. No DNA reference may hard-code a model name; only a *capability profile* (e.g. `tier=fast`, `tier=reasoning`, `tier=coding`, `tier=image_gen`).

**Operational consequence:** when a provider goes down or doubles their price, the platform updates the routing table and every running Darwin adapts without a code change.

## P7 — Every action is auditable.

Every neuron action — every LLM call, every tool call, every payment, every browser click — is logged with a tamper-evident hash chain. The user can replay any moment of their Darwin's life. The platform can prove what a Darwin did or did not do, to itself and to regulators.

**Operational consequence:** the orchestrator writes to an append-only event log before any side-effecting action commits. No log, no action.

## P8 — Kill-switches are sacred.

The owner can pause or terminate any Darwin at any time, with no confirmation required from the Darwin itself. The platform can do the same to any Darwin that violates policy. Kill commands take effect before the next neuron action, never delayed by an in-flight task.

**Operational consequence:** the orchestrator polls the kill-switch flag at every step of the main loop. There is no "graceful shutdown" delay longer than a few seconds.

## P9 — Bias toward the boring choice on infrastructure.

The novelty in this project is the evolutionary agent design. Everything else — the database, the queue, the secrets manager, the deployment platform, the web framework — should be the most boring, well-understood tool that fits. Postgres, not a new vector DB. FastAPI, not a hand-rolled HTTP layer. Standard Kubernetes, not bleeding-edge orchestration. We can't afford novelty taxes in the substrate when the superstrate is already an unsolved problem.

**Operational consequence:** when choosing between two tools, pick the one with the bigger Stack Overflow tag.

## P10 — Documentation is a deliverable, not an afterthought.

This is itself the meta-principle of this repo. The architecture, the rationale, the open questions — all of it written down before code, and kept current after. A Darwin that can read its own design docs is a Darwin that can reason about its own design.

**Operational consequence:** docs live in-repo, version-controlled, and a change to a P-rule above requires a code review the same way a schema change does.
