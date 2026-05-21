# Phase 1 — evolution loop build plan

Phase 0 proved the plumbing: one Darwin loops, dispatches, reconciles, and survives restarts. Phase 1 proves the _thesis_: a Darwin rewrites its own soma based on observed failures, the rewrite measurably improves (or is correctly rejected), and a reproduction event spawns a child that boots from inherited lessons and survives on its own.

This doc is the concrete, file-by-file build plan for Phase 1. It leans on docs [03](03-soma-and-orchestrator.md) (self-modification engine), [06](06-evolution-and-reproduction.md) (evolution + reproduction), and the Phase 0 infrastructure (doc [20](20-phase-zero-runtime-build-plan.md)).

## What Phase 1 proves

1. **Self-modification under evaluation.** A Darwin proposes a soma patch, the in-DNA harness evaluates it empirically, and the promoted patch measurably improves fitness (or the archive records a dead end correctly).
2. **Reproduction.** A Darwin that clears the fitness bar + has treasury surplus triggers distill → validate → sign → spawn → register. The child boots fresh from DNA + N+1 layers.
3. **Lineage viability.** The child (generation 2) runs its own loop, reads its inherited lessons, and demonstrates at least as good a starting position as generation 1 had.
4. **Real economic activity (small).** The content + affiliate strategy earns at least $1 USDC against real infrastructure (real LLM costs, real content publishing, synthetic traffic).
5. **Lesson legibility.** A human reviewer can read the sealed DNA layer, understand what was learned, and verify it does not weaken the constitution.

## The self-modification pipeline (what already exists vs. what's new)

### Already implemented (in `darwin_dna`)

| Component          | File                                                | Purpose                                                                          |
| ------------------ | --------------------------------------------------- | -------------------------------------------------------------------------------- |
| Proposal gate      | `orchestrator_skeleton/nodes/propose_mutation.py`   | Gated on recurring failure; calls coding-tier LLM; validates via patch_validator |
| Patch validator    | `machinery/patch_validator.py`                      | Rejects patches targeting DNA, kill-switch, audit, treasury                      |
| Evaluation harness | `machinery/eval_harness.py`                         | Frozen scenario replay; baseline vs. candidate trajectory comparison             |
| Apply mutation     | `orchestrator_skeleton/nodes/apply_mutation.py`     | Records promotion; clears recurring-failure counter                              |
| Mutation archive   | `evolution/archive.py`                              | Ring-buffer of proposals + dead-end detection                                    |
| Fitness scorer     | `machinery/fitness.py`                              | Weighted fitness: profit + diversity + escalation quality − violations           |
| Reproduction event | `evolution/reproduction.py`                         | Full PAUSE→DISTILL→VALIDATE→SIGN→SPAWN→REGISTER→RESUME sequence                  |
| Lesson distiller   | `evolution/lesson_distiller.py`                     | Deterministic, legibility-checked lesson generation                              |
| Layer compaction   | `evolution/reproduction.py::_compact`               | Deterministic fold of old layers past generation soft cap                        |
| Check reproduction | `orchestrator_skeleton/nodes/check_reproduction.py` | Fitness + surplus gating; force-reproduce support                                |

### New work for Phase 1

| Component                            | Where                                      | What                                                                                                 |
| ------------------------------------ | ------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| **Real soma sandbox**                | `darwin_platform/evolution/patch_staging.py` | Mount DNA read-only + soma read-write in an E2B microVM; apply patch in isolation before promotion   |
| **Patch staging pipeline**           | `darwin_platform/evolution/patch_staging.py` | Fresh sandbox → copy soma → apply diff → run evaluation harness → return pass/fail                   |
| **Production eval runner**           | `darwin_platform/evolution/eval_runner.py`   | Replays N recent real situations (from audit log) through the patched soma; measures fitness delta   |
| **DNA signing service (prod)**       | `darwin_platform/security/signing_service.py` | HSM-backed (AWS KMS in Phase 1); signs new layers at reproduction; exposes `PlatformSigner` protocol |
| **Lineage tree API + visualization** | `darwin_platform/api.py` extension           | `GET /darwins/{id}/lineage/tree` → JSON tree; minimal Next.js rendering                              |
| **Real content publishing**          | `darwin_dna/neurons/content_publisher.py`    | Wire WordPress API via x402; measure real published-piece count                                      |
| **Real analytics**                   | `darwin_dna/neurons/analytics_measurer.py`   | Wire Google Analytics or affiliate network dashboard; measure real revenue                           |
| **Mainnet wallet (small)**           | `darwin_platform/adapters/wallet.py`         | Base mainnet USDC; funded from our test pool ($50–100 per Darwin)                                    |
| **Extended neuron wiring**           | See § below                                  | `accounts.manager`, `infrastructure.provisioner` in real dispatch paths                              |

## The patch staging pipeline (detailed)

The Layer-0 eval harness (`machinery/eval_harness.py`) is designed for in-DNA deterministic evaluation — a pure-function replay over a frozen tape. That's sufficient for Phase 0 because the "patch" is a structured `BehaviorDelta`, not a real code diff applied to real files.

Phase 1 upgrades this to a **real code-patch staging pipeline** that:

1. **Provisions a fresh sandbox** (E2B microVM via `darwin_platform/adapters/sandbox.py`).
2. **Mounts the DNA read-only** and copies the live soma into the sandbox's writable filesystem.
3. **Applies the unified diff** from `propose_mutation`'s output against the soma copy.
4. **Runs the evaluation harness** inside the sandbox: replays N recent heartbeats from the audit log (real situations, not synthetic) through the patched orchestrator. Measures fitness before and after.
5. **Returns the verdict**: promote (fitness improved, invariants held) or reject (archived with reason).
6. **Tears down the sandbox** regardless of outcome.

The `propose_mutation` node's output shape evolves:

```python
# Phase 0 (current): structured BehaviorDelta
{"kind": "backoff", "failure_class": "rate_limit_hit", "after_failures": 3}

# Phase 1: real unified diff + BehaviorDelta summary (audit-legible)
{
  "changes": [
    {"path": "soma/strategies/content_affiliate.py",
     "diff": "--- a/soma/strategies/content_affiliate.py\n+++ b/..."}
  ],
  "behavior_delta": {"kind": "backoff", "failure_class": "rate_limit_hit", "after_failures": 3},
  "rationale": "back off from the rate-limited endpoint after 3 failures"
}
```

The evaluation harness gains a new entry point:

```
darwin_platform/
  patch_staging.py
    class PatchStagingService:
        async def stage_and_evaluate(
            self,
            darwin_id: str,
            patch: Patch,
            recent_situations: list[AuditEntry],  # from the event log
            current_fitness: float,
        ) -> StagingVerdict:
            ...
```

The supervisor calls this between `propose_mutation` and `apply_mutation`. If the verdict is `promote`, the supervisor applies the diff to the live soma at the next safe checkpoint. If `reject`, the archive records it and the loop moves on.

## Reproduction in production

The reproduction machinery already exists in the genome (`evolution/reproduction.py`). Phase 1 upgrades the platform side to make it production-real:

### DNA signing service

Phase 0 uses `SigningService` from `darwin_fakes` — an in-memory Ed25519 key. Phase 1 moves to:

```
darwin_platform/
  signing_service.py
    class KMSSigningService:
        """AWS KMS-backed signing. The key never leaves the HSM. The public
        key is cached in memory for boot-time manifest verification."""

        def __init__(self, key_id: str, region: str = "us-east-1"): ...
        def sign(self, message: bytes) -> bytes: ...
        def public_key_pem(self) -> bytes: ...
```

The `Supervisor` gains a `--signing-kms-key-id` flag. When present, `KMSSigningService` replaces the in-memory signer. All manifests — Layer 0 and every reproduction layer — are signed by the same HSM-backed key.

### Child wallet provisioning

At reproduction, `ChildSpawner.spawn()` currently creates an in-memory FakeStack for the child. Phase 1:

1. Provisions a real Coinbase Agentic Wallet on Base mainnet for the child.
2. Transfers the budget (fraction of parent's surplus) from parent wallet → child wallet on-chain.
3. Creates the child's vault namespace.
4. Registers the child in `darwin_lifecycle` persistence.
5. Starts the child's drive loop task.

The parent's wallet balance decreases by `budget_usdc` atomically with the transfer.

### Lineage tree

The lineage registry (`supervisor.py::LineageRegistry`) already records parent→child edges. Phase 1 adds:

- `GET /darwins/{id}/lineage/tree` → nested JSON representation (ancestors + descendants).
- A minimal Next.js visualization (a tree or timeline) so an operator can see "Darwin A (gen 1) → Darwin B (gen 2) → ..."

## Real economic activity

Phase 1 is where a Darwin touches real money — small, bounded, and monitored.

### The content + affiliate flow (real)

```
                  ┌──────────────────────────────────────────────┐
                  │ content_affiliate strategy (Layer 0)         │
                  └──────────────────────┬───────────────────────┘
                                         │ proposes
                                         ▼
               ┌─── Neuron dispatches (real infrastructure) ───────────┐
               │                                                       │
  ┌────────────▼───────────┐  ┌──────────────▼───────────┐  ┌─────────▼───────────┐
  │ content.writer         │  │ content.publisher        │  │ analytics.measurer  │
  │ LLM → article text     │  │ WordPress API (x402)     │  │ affiliate dashboard │
  │ cost: ~$0.02/piece     │  │ cost: ~$0.01/publish     │  │ cost: ~$0.005/poll  │
  └────────────────────────┘  └──────────────────────────┘  └─────────────────────┘
               │                          │                           │
               │                          ▼                           ▼
               │               Published blog post           Revenue measurement
               │               (real WordPress site)         (real clicks, real $)
               └──────────────────────────┤
                                          ▼
                             Treasury ledger (mainnet USDC)
```

**Key constraints:**

- Starting fund per Darwin: $50–100 USDC from our test pool.
- Hard cap per action: $2 (Layer 0 inviolable).
- Hard cap lifetime: $500 (Layer 0 inviolable).
- Target: earn ≥ $1 revenue within 7–14 days (proving the concept, not profitability).
- Traffic: initially synthetic (we drive clicks to our own site); later organic.

### Extended neuron catalog (Phase 1 additions)

| Neuron                       | Purpose                                     | Status                                                    |
| ---------------------------- | ------------------------------------------- | --------------------------------------------------------- |
| `content.publisher`          | Posts to WordPress via x402 API             | **Implemented** (Layer 0); wire real endpoint             |
| `accounts.manager`           | Handles signup flows; escalates on captchas | **Implemented** (Layer 0); wire real browser path         |
| `analytics.measurer`         | Reads metrics from affiliate dashboards     | **Implemented** (Layer 0); wire real endpoint             |
| `infrastructure.provisioner` | Requests managed resources from platform    | **Implemented** (Layer 0); wire real provisioning backend |
| `research.scout`             | Web search + synthesis via x402 API         | **Implemented** (Layer 0); wire real search endpoint      |
| `vault.writer`               | Centralized vault writes for credentials    | **Implemented** (Layer 0); wire real vault                |

All neurons already exist in the genome; Phase 1's work is **wiring real backends** and confirming dispatch success rates ≥ 80% against real infrastructure.

## Metering and cost governance

Phase 1 automates cost tracking (Phase 0 was manual):

| Metric               | Source                             | Dashboard                                     |
| -------------------- | ---------------------------------- | --------------------------------------------- |
| LLM cost/heartbeat   | LiteLLM proxy usage endpoint       | Prometheus gauge `darwin_llm_cost_usdc`       |
| Neuron cost/dispatch | `NeuronResult.cost_actual_usdc`    | Prometheus counter `darwin_neuron_cost_total` |
| Total daily spend    | Wallet ledger + billing service    | Prometheus gauge `darwin_daily_spend_usdc`    |
| Revenue (affiliate)  | `analytics.measurer` neuron output | Prometheus gauge `darwin_revenue_usdc`        |
| Cost per Darwin/day  | Billing sweep (existing)           | Alert if > $20/day                            |

The billing service (`billing.py`) already meters wallet debits. Phase 1 adds a Prometheus exporter so Grafana can visualize spend vs. revenue trajectories.

## Operational changes

### Sandbox lifecycle

Phase 1 uses E2B microVMs in two roles:

1. **Primary Darwin sandbox** — where the live soma runs (existing from Phase 0).
2. **Patch staging sandbox** — ephemeral; spun up at `propose_mutation`, torn down after evaluation.

The supervisor's throttle service already enforces sandbox quotas per Darwin. Phase 1 adds a separate quota for staging sandboxes (default: max 3 concurrent staging evaluations fleet-wide; configurable).

### Mainnet safety

Real USDC on Base mainnet introduces new failure modes. Protections:

- **Wallet-level hard caps** at the Coinbase Agentic Wallet policy layer (doc 17) — independent of our treasury policy.
- **Sanctions screening** on every outbound transfer (already implemented; `sanctions.py`).
- **Transfer confirmations** — the supervisor waits for on-chain confirmation before updating the ledger.
- **Manual daily sweep** — during early Phase 1, a human reviews the fleet's wallet balances daily. Automated alerting replaces this within 2 weeks.

### Cross-pollination (deferred, infrastructure laid)

The cross-pollination archive (`cross_pollination.py`) is already implemented. Phase 1 does **not** activate it — each lineage evolves independently. But the architecture supports it when Phase 3 opts in.

## Definition of done (Phase 1)

Per `docs/overview/06-roadmap-and-phases.md`:

- [ ] A Darwin self-modifies at least once: the modification passes the patch staging evaluation, and post-promotion fitness measurably improves (or the modification is correctly rejected and archived).
- [ ] A reproduction event completes: child spawns with new DNA layer, boots fresh soma, runs its own loop, inherits lessons.
- [ ] The lineage tree shows 2+ generations (parent → child, both alive or child alive after parent death).
- [ ] The Darwin earns at least $1 USDC of real revenue (affiliate commission or equivalent).
- [ ] The audit trail reconstructs all of the above: every mutation proposal, evaluation verdict, reproduction step, and financial transaction is hash-chained and replayable.
- [ ] A human reviewer reads the sealed DNA layer (generation 2) and confirms: the lessons are legible, the constitution is not weakened, and each lesson traces back to a specific runtime observation.

### Kill criteria (Phase 1)

- After 16 weeks, no successful self-modification has measurably improved fitness, and we cannot identify why.
- OR: every Darwin we spawn dies of bankruptcy before earning anything, and we can't find a viable starting strategy in Layer 0.

## Module map (new + modified files)

```
src/
  darwin_platform/
    evolution/
      patch_staging.py            NEW  — sandbox-based patch staging + evaluation
      eval_runner.py              NEW  — replays real audit entries through patched soma
    security/
      signing_service.py          NEW  — KMS-backed PlatformSigner (AWS KMS / HSM)
    runtime/
      supervisor.py               MOD  — patch staging integration, mainnet wallet provisioning
    api.py                        MOD  — /lineage/tree endpoint, staging status
    adapters/wallet.py            MOD  — mainnet mode, on-chain transfer confirmations
    observability/monitoring.py   MOD  — cost governance metrics (LLM, neuron, daily spend)

  darwin_dna/
    orchestrator_skeleton/
      nodes/propose_mutation.py   MOD  — emit real unified diffs (not just BehaviorDelta)
      nodes/evaluate_mutation.py  MOD  — call platform staging service instead of in-process harness
    machinery/eval_harness.py     MOD  — accept real audit entries for replay (not just synthetic)
    neurons/content_publisher.py  MOD  — wire real WordPress API endpoint
    neurons/analytics_measurer.py MOD  — wire real analytics endpoint

  tools/
    soak/                         (from Phase 0)
    evolution_test/               NEW  — end-to-end evolution test harness
      run_evolution.py            Entry: spawn, run until mutation OR reproduction, assert criteria
      assertions.py              Per-criterion checks (reusable in CI)
```

Doc 08 §"Module layout" carries the canonical bounded-context map; the
paths shown here use the same scheme.

## Timeline and sequencing

Phase 1 builds on Phase 0's passing soak test. The work splits into three tracks that can partially overlap:

| Track                          | Weeks | Deliverable                                                                                                     |
| ------------------------------ | ----- | --------------------------------------------------------------------------------------------------------------- |
| **A — Patch staging**          | 8–10  | `patch_staging.py`, `eval_runner.py`, modified `propose_mutation`/`evaluate_mutation`, end-to-end mutation test |
| **B — Reproduction + signing** | 9–12  | `signing_service.py` (KMS), mainnet wallet provisioning, lineage tree API/UI, end-to-end reproduction test      |
| **C — Real economic activity** | 10–14 | Real WordPress site, real affiliate links, wired neurons, cost dashboard, revenue measurement                   |

Tracks A and B are the engineering core; track C provides the empirical evidence that the whole thing earns money.

## After Phase 1

When the definition of done is met, the next doc is **Phase 2 — multi-tenant alpha** (doc `22-phase-two-multi-tenant-build-plan.md`): real users, Vault Enterprise namespaces, per-Darwin Kubernetes isolation, the full payment stack, and the user-facing dashboard.
