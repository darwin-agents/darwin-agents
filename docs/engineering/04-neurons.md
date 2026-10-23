# Neurons

Neurons are the specialized worker sub-agents that actually do things in the world. The orchestrator plans; neurons act.

## The neuron contract

Every neuron — old or newly evolved, written by humans or by a Darwin — implements the same interface. Without a stable contract, the orchestrator cannot reason about which neuron to call, and the self-modification engine cannot evaluate alternatives.

```python
class Neuron(Protocol):
    name: str
    version: str
    capability_tags: set[str]        # e.g. {"web.publish", "content.long_form"}
    cost_estimate: CostEstimate      # tokens, $$, latency, per typical invocation
    failure_modes: list[FailureMode] # known classes of failure, with escalation hints

    async def plan(self, task: Task, context: NeuronContext) -> Plan:
        """Look at the task and return a plan. Cheap. No side effects."""

    async def execute(self, plan: Plan, context: NeuronContext) -> NeuronResult:
        """Do the work. May call out to the world. Side effects allowed."""

    async def escalate(self, blocker: Blocker, context: NeuronContext) -> EscalationTicket:
        """When stuck (captcha, KYC, approval), file a ticket for the owner."""
```

The contract is enforced by a base class in the DNA. A neuron that doesn't conform is rejected at load time.

## The initial neuron catalog (layer 0)

The founding Darwin ships with a small, deliberately incomplete set of neurons. We want it to discover gaps and request new neurons (which we then build, or which a future generation generates).

| Neuron | What it does | Tier | Notes |
|---|---|---|---|
| **content.writer** | Long-form text generation (blogs, posts, product descriptions) | LLM (reasoning) | Routed through the platform LiteLLM gateway; auto-picks cheapest model that meets quality bar |
| **content.publisher** | Posts content to configured destinations (WordPress, Medium, Ghost, X, LinkedIn) | API + browser fallback | Uses Browser-Use when no API exists; promotes to cached Stagehand replay once stable |
| **browser.operator** | Generic browser-driving neuron | Browser-Use on Browserbase | Used for any site without a stable API; cached replay for repetitive flows |
| **accounts.manager** | Signs up for services, holds session cookies, refreshes tokens | Browser + Vault | This is the captcha-hungry one; escalates often |
| **ads.manager** | Buys and optimizes ad spend (Meta, Google, X) | API | Hard-capped by treasury policy; every spend over $5 escalates in v1 |
| **trading.operator** | Trades financial and crypto markets; takes positions in prediction and betting markets | API | Live positions, bounded only by the Darwin's treasury hard caps and the kill switch — like any other spend |
| **support.responder** | Responds to inbound DMs, emails, support tickets | LLM + integrations | Routes to escalation on anything legal-sounding or angry |
| **analytics.measurer** | Reads metrics from configured destinations, computes ROI | API | Feeds the orchestrator's reflect node |
| **research.scout** | Web-searches and synthesizes for the strategy engine | Search API + LLM | Cheap-tier model; cached aggressively |
| **payments.facilitator** | Handles outgoing payments — APIs, agent services, infrastructure | x402 (USDC via the Darwin's wallet) | The only neuron with treasury write access. See [`05-treasury-and-payments.md`](05-treasury-and-payments.md) |
| **infrastructure.provisioner** | Requests new infrastructure from the platform — databases, S3 buckets, vector stores, scheduled jobs | Platform API | The only neuron that can ask the platform to provision new managed resources. Writes the resulting credentials to the Darwin's vault. |
| **vault.writer** | Stores account credentials, API keys, and lesson artifacts the Darwin wants to persist | Vault SDK | Other neurons request "remember this" through this neuron rather than writing the vault directly. Centralizes vault writes for auditing. |
| **compliance.kyc_submitter** | Submits an owner's KYC document to a verification provider | Vault (SENSITIVE tier) + HTTP | The only neuron that performs a SENSITIVE-tier read; it escalates with a `kyc` blocker, waits for the owner's single-use grant, and returns only the outcome, never the document (`src/darwin_dna/neurons/compliance_kyc.py`). |

A Darwin in production rarely uses all of them at once. The strategy engine picks the subset relevant to whichever business archetype is currently active.

## Why this taxonomy

Three properties shaped the catalog:

1. **Each neuron has one obvious failure mode.** content.publisher fails on auth and rate limits. accounts.manager fails on captchas. ads.manager fails on policy review. We want failures to map to a single neuron so escalation routing is unambiguous.
2. **Browser is a substrate, not a domain.** browser.operator is general-purpose; domain neurons (content.publisher, accounts.manager) *use* it when no API exists. This avoids 50 copies of the same Playwright boilerplate.
3. **Money flows through one neuron only.** payments.facilitator is the sole writer to the treasury. Every other neuron requesting a spend goes through it, which means treasury policy is enforced in exactly one place.

## How a new neuron gets added

There are three paths:

**Path A — Human-authored, blessed by the platform.** The Darwin team writes a new neuron, audits it, signs it, and adds it to the next layer-0 release. All new Darwins spawned after that have it. Existing Darwins receive it via a forced reproduction event if the platform decides it's important enough.

**Path B — Discovered by a Darwin.** A running Darwin identifies a recurring gap ("I keep needing to interact with Stripe Connect and my browser neuron is unreliable"). The orchestrator files a *capability request* — essentially a structured TODO that ends up in the platform's neuron backlog. Humans then build it via Path A.

**Path C — Generated by a Darwin (experimental, phase 3).** A Darwin's self-modification engine writes a new neuron client and registers it. This is the most dangerous path because the neuron has side-effect privileges. The validator subjects generated neurons to extra scrutiny: a quarantine period running only against staging APIs, mandatory human review before any production traffic, and a tighter cost ceiling. We will not enable Path C in the initial launch.

> **As built (2026-10):** Path C is implemented but off by default. `NeuronProposal` + the admission gate live in `src/darwin_dna/machinery/neuron_proposal.py`; admitted neurons are named `learned.<slug>`, run as interpreted steps under the sandbox, and wait for an operator via `POST /platform/darwins/{id}/proposals/{name}/admit|reject`. The switches are the DNA hyperparameters `path_c_enabled = False` and `path_c_require_human_review = True` (`src/darwin_dna/hyperparameters.py`).

## Routing — orchestrator picks the neuron

The orchestrator's dispatch node asks: "Given this task, which neuron with which `capability_tags` is the best fit?" Selection is currently:

1. Filter to neurons whose `capability_tags` are a superset of the task's required tags.
2. Among those, prefer neurons with the lowest expected cost that meets the task's quality requirement.
3. Break ties by recency of success — a neuron that just succeeded on a similar task wins.
4. If no neuron matches, file a capability request and try the closest available fallback.

The cost/quality decision logic is itself part of the soma and therefore evolvable. The selection algorithm itself is in the DNA.

## Neuron isolation

Each neuron invocation runs in its own sandboxed E2B microVM. The neuron sees:

- Its own code (mounted RO from the soma)
- A minimal Python environment
- Outbound network access through the platform's egress proxy (filtered, audited)
- A scoped credential set from the owner's vault (only the secrets relevant to this neuron's task)
- A scratch directory (wiped at end)

It does **not** see:

- The DNA core
- The treasury private key
- Other neurons' code or state
- The owner's full vault
- The orchestrator's working memory beyond what's passed in the task

This is overkill for some neurons (analytics is read-only) but the consistency is worth it: every neuron is treated as potentially malicious, because over time Darwin-generated neurons might be.

## Costing

Every neuron declares a `CostEstimate` per invocation:

```python
@dataclass
class CostEstimate:
    usd_p50: float           # median expected USD cost (LLM tokens, API fees, ad spend, etc.)
    usd_p95: float           # 95th-percentile expected USD cost
    latency_p50_ms: int
    latency_p95_ms: int
    escalation_probability: float  # rough prior on hitting a human-needed blocker
```

Estimates are calibrated from historical data, kept in the per-Darwin database, and updated by the reflect node after each invocation. The orchestrator uses them to plan — "this strategy will cost about $0.40 to attempt, with 18% chance of escalation" — and the treasury policy uses them to refuse plans whose worst-case cost exceeds the remaining safety margin.
