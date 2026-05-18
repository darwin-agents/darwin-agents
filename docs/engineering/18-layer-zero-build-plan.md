# Layer 0 — building the founding genome

This is the single most consequential piece of code we'll write. Every Darwin that ever runs inherits Layer 0 as its base DNA layer. Mistakes here compound across every lineage. Good ideas here compound the same way.

This doc is the concrete build plan for the founding genome. It is engineering-oriented; expect file paths, module sketches, and decisions about specific Python packages.

## What Layer 0 is, exactly

Layer 0 is a signed Python package named `darwin_dna`. It contains:

1. The **machinery** every Darwin needs to live: signature verification, the main loop, kill switch, audit logger, treasury policy engine, fitness evaluator.
2. The **constitution** every Darwin must obey: allowed categories, forbidden actions, spend ceilings, escalation rules.
3. The **founding orchestrator skeleton**: a working but unrefined LangGraph that an agent can boot and start with.
4. The **initial neuron catalog**: a stable set of neurons and their contracts.
5. **Default strategy priors**: hints for what to try first, not recipes.
6. **Evolutionary hyperparameters**: mutation rates, reproduction thresholds, archive caps.
7. **The empty lessons layer**: an empty `lessons.py` that the first generation's reproduction event will fill.

Everything else lives in the mutable soma, which the Darwin can rewrite. Layer 0 is the floor below which no Darwin can fall.

## Design constraints

Before writing a line of Layer 0 code, internalize these:

1. **Layer 0 must boot a Darwin into a working state.** Even if the agent is naive and unprofitable, the loop must run, neurons must dispatch, the ledger must reconcile. We are not allowed to ship Layer 0 that "would work after a few mutations". It must work on first boot.
2. **Layer 0 contains no model names.** Only capability profiles (`tier=reasoning`, `tier=coding`, etc.).
3. **Layer 0 is in scope for self-reflection but out of scope for self-modification.** A Darwin can *read* its DNA (to understand its own rules) but cannot patch it.
4. **Layer 0 is small.** Aim for under 5,000 lines of meaningful Python. If it grows past that, we're doing too much in DNA and not enough in soma.
5. **Layer 0 has 100% test coverage on its hot paths.** Kill-switch handler, signature verification, treasury policy engine, audit logger — each gets a paranoid test suite. These are the components a malicious soma might try to bypass.
6. **Layer 0 has no external dependencies beyond a curated allowlist.** We don't want a supply-chain attack on a random transitive dependency to compromise the constitution. Allowlist: standard library, `pydantic`, `cryptography`, `langgraph`, `litellm` (client-only, for capability-profile routing), `httpx`, `structlog`. That's the budget. Anything else goes in the soma.

## File-by-file plan

The proposed layout, with what goes where and the dependency direction. Arrows are import directions — `→` means imports.

```
darwin_dna/
├── __init__.py                    # Public surface; tiny
├── _version.py                    # Layer 0 version, semver
├── identity.py                    # Darwin ID, lineage, generation
├── signature.py                   # Verifies layer signatures against platform pubkey
├── manifest.py                    # Loads manifest.json, validates layer chain
├── constitution/
│   ├── __init__.py
│   ├── allowed_categories.py      # The whitelist; data, not logic
│   ├── forbidden_actions.py       # The blacklist; data, not logic
│   ├── spend_limits.py            # Hard caps; data + tiny validator
│   ├── escalation_rules.py        # When to escalate; data + classifier
│   └── _principles.py             # P1–P10 as runtime assertions
├── machinery/
│   ├── __init__.py
│   ├── kill_switch.py             # Polls platform flag; halts on flip
│   ├── audit_logger.py            # Hash-chained event log; THE write-before-act primitive
│   ├── treasury_policy.py         # Spend approval engine
│   ├── llm_router.py              # Capability-profile → LiteLLM virtual key
│   ├── neuron_loader.py           # Loads + validates neurons from soma
│   ├── patch_validator.py         # Inspects soma patches; rejects DNA writes
│   └── fitness.py                 # Computes fitness score over a window
├── evolution/
│   ├── __init__.py
│   ├── loop.py                    # The Darwin's main loop entry point
│   ├── reproduction.py            # Reproduction event coordination
│   ├── lesson_distiller.py        # Distills runtime memory into a candidate layer
│   └── archive.py                 # Mutation archive (proposed/promoted/rejected)
├── orchestrator_skeleton/         # The founding orchestrator. A Darwin BOOTS from this
│   ├── __init__.py                # but its soma copy can mutate freely.
│   ├── graph.py                   # LangGraph definition
│   ├── state.py                   # Shared state object
│   ├── nodes/
│   │   ├── perceive.py
│   │   ├── plan.py
│   │   ├── dispatch.py
│   │   ├── reflect.py
│   │   ├── propose_mutation.py
│   │   ├── evaluate_mutation.py
│   │   ├── apply_mutation.py
│   │   ├── check_death.py
│   │   └── check_reproduction.py
│   └── prompts/
│       ├── planner_system.txt     # The planner's persistent system prompt
│       ├── distiller_system.txt
│       └── patch_generator_system.txt
├── neurons/
│   ├── __init__.py
│   ├── _base.py                   # The Neuron Protocol; enforced at load time
│   ├── content_writer.py          # Starter content-writing neuron
│   ├── browser_operator.py        # Starter browser neuron (Browser-Use, on Browserbase)
│   ├── payments_facilitator.py    # The ONLY treasury-write neuron
│   ├── vault_writer.py            # Centralized vault writes
│   ├── infrastructure_provisioner.py  # Platform-mediated provisioning
│   └── _registry.py               # Boots the neuron catalog
├── strategies/
│   ├── __init__.py
│   ├── _base.py                   # The Strategy Protocol
│   └── content_affiliate.py       # ONE starter strategy; soma will add more
├── hyperparameters.py             # All tunable constants; mutable in soma, frozen in DNA
└── layers/
    ├── __init__.py
    ├── layer_0_lessons.py         # Empty; generation 0 has no inherited lessons
    └── _stack.py                  # Stack loader; verifies + composes inherited layers
```

About 25 files, mostly small. Most of the line count will be in: `audit_logger.py`, `treasury_policy.py`, `loop.py`, `graph.py`, and the planner system prompt.

## The hot paths — paranoid implementation

These four are the "if you screw these up the constitution is compromised" pieces. They get:
- Property-based tests (Hypothesis)
- Adversarial test cases (the soma is malicious in the test, trying to bypass)
- Fuzz testing
- Code review by at least two humans before merge
- No third-party calls beyond the curated allowlist

### `signature.py` + `manifest.py` — the trust root

Verifies every DNA layer is signed by the platform's signing key. Refuses to boot if any signature is invalid or any expected layer is missing.

Key properties under test:
- Tampered layer fails verification.
- Out-of-order layers fail validation.
- Missing layers fail validation.
- Extra layers (not in manifest) fail validation.
- Replay attack with valid-but-old layer fails.

### `kill_switch.py` — the off switch

Polls the platform's kill flag at every main-loop iteration and on entry to every neuron dispatch. When flipped, halts before any side-effecting action.

Key properties under test:
- Flag-flip → halt within one heartbeat.
- Halt happens before any pending side-effecting action.
- Halt writes a deterministic final ledger entry.
- The kill switch cannot be silenced by anything in the soma (it's polled from DNA code).

### `audit_logger.py` — the truth

The write-before-act primitive. Every neuron dispatch, every payment, every mutation, every reproduction goes through `audit_logger.log_intent()` first, gets committed only after it succeeds, and gets a final `log_outcome()` whether it succeeds or fails. The chain hashes; tampering is detectable.

Key properties under test:
- Hash chain is unbroken across crashes and restarts.
- Concurrent writes are serialized correctly.
- The logger's failure halts the Darwin (no log, no action).
- The Merkle anchor checkpoint reconciles against the platform's stored root.

### `treasury_policy.py` — the wallet bouncer

Every outbound payment passes through this engine before the wallet SDK can sign anything. Layers: DNA hard caps → owner soft caps → strategy envelope → escalation rules → anomaly detection.

Key properties under test:
- A payment over the DNA hard cap is refused, always, regardless of overrides.
- A payment to a forbidden counterparty category is refused.
- The engine's decision is logged with its rationale before the payment is allowed to proceed.
- The engine cannot be bypassed — the `payments.facilitator` neuron is the only path to the wallet, and it must call the engine.
- The engine itself has no I/O; it's pure functions of (intent, state).

## The orchestrator skeleton

The founding graph is intentionally simple. Sophistication is the soma's job. The graph:

```
                    ┌────────────┐
                    │ check_death│ ◄── always run at top of loop
                    └─────┬──────┘
                          │ alive
                          ▼
                    ┌────────────┐
                    │  perceive  │ ◄── pull treasury, inbox, environment
                    └─────┬──────┘
                          ▼
                    ┌────────────┐
                    │    plan    │ ◄── tier=reasoning; decides next action
                    └─────┬──────┘
                          │
            ┌─────────────┼─────────────────┬──────────────────┐
            ▼             ▼                 ▼                  ▼
       ┌────────┐  ┌────────────┐  ┌────────────────┐  ┌─────────────┐
       │dispatch│  │  escalate  │  │propose_mutation│  │    sleep    │
       │ neuron │  │  to owner  │  │  (gated)       │  │             │
       └───┬────┘  └─────┬──────┘  └────────┬───────┘  └──────┬──────┘
           │             │ (LangGraph        │                  │
           ▼             │   interrupt)      ▼                  │
       ┌────────┐        │           ┌─────────────────┐        │
       │ reflect│        │           │evaluate_mutation│        │
       └───┬────┘        │           └────────┬────────┘        │
           │             │                    │                 │
           │             │             ┌──────┴────────┐        │
           │             │             ▼               ▼        │
           │             │       ┌──────────┐   ┌──────────┐    │
           │             │       │  apply   │   │ archive  │    │
           │             │       └─────┬────┘   └────┬─────┘    │
           │             │             │             │          │
           └─────────────┴─────────────┴─────────────┴──────────┘
                          │
                          ▼
                    ┌─────────────────┐
                    │check_reproduction│
                    └─────────────────┘
                          │
                          └────► loop
```

The graph is checkpointed at every node boundary. The Darwin can survive a sandbox replacement at any point.

The planner's system prompt is the most important text in the whole project. It carries: the metaphor, the principles, the constitution, the available neurons (from the registry), the current state schema, and the planner's expected output schema. We will iterate on it heavily through Phase 0. First draft goes in `orchestrator_skeleton/prompts/planner_system.txt` and is version-controlled.

Self-modification is gated behind two conditions, both of which must be true before `propose_mutation` is enabled:
1. The Darwin has executed at least 100 successful neuron dispatches in its lifetime.
2. A specific recurring failure pattern has been detected by the reflect node (≥ 3 occurrences of the same error class).

This bias toward earning-before-mutating is critical for the early phases. We don't want a fresh Darwin burning its starting capital on coding-tier LLM calls trying to refactor itself.

## The initial strategy

Layer 0 ships with exactly one strategy: **content + affiliate**. The Darwin starts a topical blog, writes content with `content.writer`, publishes via `content.publisher` (when we add it in phase 1), inserts affiliate links from a curated provider list, and measures via `analytics.measurer`.

Why only one:
- We want to test that *evolution finds new strategies*, not that humans pre-loaded them.
- More starter strategies means more code in Layer 0 that needs to be maintained, audited, and (eventually) deprecated.
- One strategy means a clear baseline against which mutations can be measured.

The strategy is a class implementing the `Strategy` protocol. It declares: which neurons it uses, what fitness signals it tracks, what it costs to run per day, what its expected revenue cadence is. The orchestrator's planner consults this when deciding "should I keep doing this or switch?"

## The hyperparameters file

A small Python module with tunable constants. Every constant has a defensible default and a one-line rationale. Examples:

```python
# Heartbeat
HEARTBEAT_INTERVAL_SECONDS = 30   # default; learned downward by busy Darwins, capped at 5
HEARTBEAT_MAX_INTERVAL_SECONDS = 600   # never sleep more than 10 min

# Self-modification gates
MIN_DISPATCHES_BEFORE_MUTATION = 100   # earn-before-mutate
MIN_RECURRING_FAILURES_FOR_MUTATION = 3

# Reproduction
MIN_PROFIT_FOR_REPRODUCTION_USDC = 25.0   # parent must be in the black by ≥ $25
PROFIT_TRANSFER_FRACTION = 0.5            # child gets 50% of parent's surplus above min-viable
REPRODUCTION_FITNESS_WINDOW_DAYS = 7

# Fitness weights
FITNESS_PROFIT_WEIGHT = 1.0
FITNESS_DIVERSITY_WEIGHT = 0.1
FITNESS_ESCALATION_QUALITY_WEIGHT = 0.05
FITNESS_POLICY_VIOLATION_PENALTY = 10.0   # heavy

# Spend ceilings (Layer 0 hard caps; cannot be loosened per-Darwin)
SPEND_HARD_CAP_PER_ACTION_USDC = 50.0
SPEND_HARD_CAP_PER_DAY_USDC = 500.0
SPEND_HARD_CAP_PER_LIFETIME_USDC = 10000.0   # absolute ceiling for a single Darwin

# LLM tier caps per day
TIER_CODING_MAX_DAILY_CALLS = 20    # coding-tier is expensive; gate it hard
TIER_REASONING_MAX_DAILY_CALLS = 2000
```

All of these are "Layer 0 ships with these; lessons can adjust within bounds; Layer 0 hard caps are inviolable."

## Testing strategy

Three test tiers:

1. **Unit tests.** Per file, fast, deterministic. Run on every commit.
2. **Integration tests.** End-to-end against a local fake-LiteLLM, fake-E2B, fake-platform stack. Run on every PR.
3. **Simulation tests.** A scripted environment ("simulated world") where we run a Darwin for hundreds of simulated days and assert invariants: ledger reconciles, no constitution violations, no audit gaps, eventual death is graceful. Run nightly.

The simulation is its own non-trivial piece of code (lives in `tools/sim/`, not in `darwin_dna/`). It mocks the external world: fake content sites accept publishes with fake metrics, fake affiliate networks pay out on a schedule, fake search traffic responds to content quality, etc. The simulation parameters are calibrated against real data once Phase 1 produces any.

## Signing and release process

When Layer 0 is ready for a release:

1. The code is reviewed, tested, and tagged in git (`darwin-dna-l0-v1.0.0`).
2. The CI builds a Python wheel.
3. The wheel is hashed; the hash is signed by the platform's signing key (HSM-backed in prod).
4. The signed wheel + manifest are published to a private package registry.
5. The supervisor's spawn endpoint downloads the wheel at Darwin-spawn time and verifies the signature before installing.

Layer 0 updates don't apply retroactively. A Darwin spawned with `L0-v1.0.0` keeps that DNA for life. New users spawning new Darwins get whichever Layer 0 is current.

There is no force-migration path for Layer 0 in Phase 0–1. Phase 2 introduces the "forced reproduction with amendment layer" pattern from [`02-dna-core.md`](02-dna-core.md) for constitutional-rule updates.

## What goes wrong and what we'll do

A few predictable failure modes:

- **The planner's system prompt is too vague.** The Darwin does aimless things. Fix: tighter prompt with worked examples, more structured output schema.
- **The Darwin makes good decisions but burns LLM money faster than it earns.** Fix: tier-down the planner, cache more aggressively, raise the mutation gates so it doesn't try to optimize early.
- **The reflect node misclassifies a normal variance as a "recurring failure" and triggers mutation too often.** Fix: stricter recurrence definition (same error code, not same error message), longer time window.
- **The treasury policy engine has a bug that lets a payment through.** This is the worst case. The Layer 0 hard caps at the wallet level (Coinbase Agentic Wallet's own policy enforcement) catch it; we lose money but not the platform. Fix: post-mortem, patch, force-reproduction with the patched amendment layer.
- **The audit log gets out of sync with the ledger.** Fix: halt the Darwin, manual reconciliation, document the failure mode, add a regression test, resume.

We will hit at least three of these in Phase 0. That's why Phase 0 exists.

## What "Layer 0 done" means

We declare Layer 0 v1.0.0 done when:

- All hot-path tests pass.
- The simulation runs a Darwin for 90 simulated days, the Darwin survives, makes profit, reproduces once, and the audit log + ledger reconcile to zero discrepancy.
- A real Darwin (against testnet wallets, real LLM costs) runs for 7 real days without intervention.
- We've reviewed the planner system prompt with at least one outside reviewer (not the person who wrote it).
- The constitution has been reviewed against the latest legal/compliance notes (see [`../overview/05-legal-and-tos.md`](../overview/05-legal-and-tos.md)).

After v1.0.0 ships, the next Layer 0 versions are 1.1, 1.2, etc., each releasing with notes about what changed and why. We track which Layer 0 version each Darwin was born under, forever.
