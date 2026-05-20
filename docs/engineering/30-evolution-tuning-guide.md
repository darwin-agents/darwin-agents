# Evolution tuning guide

How to read the mutation archive, interpret fitness trajectories, calibrate hyperparameters, and diagnose an evolution loop that isn't working. This doc is useful from Phase 1 onward — when real self-modification and reproduction are live.

## The fitness function

```
fitness = α·normalized_profit + β·diversity_bonus + γ·escalation_quality − δ·policy_penalty
```

| Weight | Symbol | Default | Effect |
| ------ | ------ | ------- | ------ |
| Profit | α | 1.0 | Dominant signal. Normalized to starting capital, so a $5 profit on a $50 start = 0.1. |
| Diversity | β | 0.1 | Small reward for sampling > 1 strategy archetype. Saturates quickly (1 − 1/n). |
| Escalation quality | γ | 0.05 | Reward for good escalations (owner approved). Penalty for pestering (owner rejected). Range: [-1, 1]. |
| Policy violations | δ | 10.0 | Massive penalty per violation. One violation typically dominates the score. |

**Key properties:**
- Profit is normalized to starting capital, not absolute. A child starting with $25 is evaluated fairly against a parent that started with $60.
- There is no volatility penalty. Ruinous variance kills the Darwin (treasury hits zero); double-penalizing it would bias toward mediocrity.
- The weights are DNA constants — not evolvable. This is the primary anti-wireheading mechanism (doc 10).

### Reading a fitness score

| Score range | Interpretation |
| ----------- | -------------- |
| < 0 | Losing money or committing policy violations. Not viable for reproduction. |
| 0.0 – 0.05 | Break-even or barely profitable. The Darwin is surviving but not thriving. |
| 0.05 – 0.20 | Healthy. The Darwin earns more than it spends over the fitness window. |
| > 0.20 | Strong. Likely ready for reproduction if surplus exists. |
| Negative despite profit | Policy violations are dominating. Investigate what's triggering δ. |

### The fitness window

Fitness is computed over a rolling window (default: 7 days, `reproduction_fitness_window_days`). This means:

- A one-day profit spike doesn't trigger reproduction.
- A 7-day sustained profit does.
- "Skill survives a window; luck usually doesn't" (doc 06).

## The mutation archive

### What it stores

The `MutationArchive` is a ring buffer (bounded at `archive_max_entries`) of every mutation attempt:

```python
ArchiveEntry(
    ts=1700000000.0,
    failure_class="rate_limit_hit",   # which recurring failure triggered this
    outcome="rejected",               # "proposed" | "promoted" | "rejected"
    rationale="back off after 3 rate limit failures"
)
```

### Reading the archive

```python
archive = runtime.mutation_archive

# All entries (oldest first):
for entry in archive.all():
    print(f"{entry.ts}: {entry.failure_class} → {entry.outcome}")

# Dead ends (failure classes whose last attempt was rejected):
dead = archive.dead_ends()
# → {"rate_limit_hit", "api_revoked"}
```

### What "dead ends" means

A failure class is a dead end when:
1. The Darwin proposed a mutation to fix it.
2. The evaluation harness ran the patched soma against the scenario.
3. The patched version didn't improve (or regressed).
4. The archive recorded `outcome="rejected"`.

The next time this failure class recurs, `propose_mutation` skips it — it's already been tried and failed. This prevents infinite retry loops on unfixable problems.

### Healthy archive patterns

| Pattern | Interpretation |
| ------- | -------------- |
| Few entries, mostly "promoted" | The Darwin is making good patches. Evolution is working. |
| Many entries, mostly "rejected" | The Darwin is trying hard but the LLM is proposing bad patches. Consider: is the failure class actually fixable by code? |
| Growing dead_ends set | The Darwin is learning what it can't fix. Normal for early generations. |
| Same failure_class appearing repeatedly (not dead-ended) | The rejection was overturned by a later promoted patch. The Darwin found a better approach on retry. |
| Archive at max capacity (ring buffer cycling) | Old entries are falling off. This is fine — the most recent signals are the relevant ones. |

### Unhealthy archive patterns

| Pattern | Problem | Fix |
| ------- | ------- | --- |
| Mutations proposing every heartbeat | Gate too loose | Raise `min_dispatches_before_mutation` |
| Every proposal is immediately rejected | Validator too strict OR LLM proposals are off-target | Review rejected patches: are they in-scope but bad (LLM quality issue), or targeting DNA paths (validator correctly blocking)? |
| Dead ends growing but fitness stagnant | The Darwin can't fix its problems by code | Check: is the issue external (API down, market changed) rather than fixable by soma patches? |
| No entries at all after many heartbeats | Gate never opening | Verify `recurring_failures` dict is accumulating (the reflect node classifies failures correctly) |

## Reproduction diagnostics

### When does reproduction fire?

The `check_reproduction` node gates on two conditions:

1. **Fitness threshold:** `fitness > 0` over the rolling window.
2. **Surplus threshold:** `profit_usdc >= min_profit_for_reproduction_usdc` (default $25).

Both must be true simultaneously. If the Darwin is fit but broke, no reproduction. If the Darwin is rich but unfit (got lucky once), no reproduction.

### The reproduction sequence

```
1. PAUSE parent (safe checkpoint)
2. DISTILL lessons from runtime memory → candidate DNA layer
3. VALIDATE layer (legibility, constitution safety)
4. SIGN layer (platform HSM)
5. SPAWN child (fresh soma, inherited DNA + new layer)
6. REGISTER child in lineage
7. RESUME parent (minus transferred budget)
```

### Common reproduction failures

| Failure | Cause | Visible signal | Resolution |
| ------- | ----- | -------------- | ---------- |
| Distillation error | Lesson violates constitution OR not legible | `DistillationError` in audit log | Review what the distiller produced; the filter is catching something. Usually a too-specific lesson that references a spend cap. |
| Fitness never reaches threshold | Darwin is break-even at best | Fitness gauge flatlined near 0 | Review the strategy: is the revenue model viable? Is the Darwin spending too much on LLM? |
| Surplus never reaches threshold | Revenue exists but costs eat it all | Balance growing too slowly | Reduce LLM tier usage; promote the Darwin to a more efficient heartbeat interval |
| Parent dies before reproduction | Treasury depleted | `event:death` in audit log | Increase `opening_usdc` or reduce `heartbeat_interval_seconds` |
| Child dies immediately | Inherited budget too small | Child's balance → 0 within hours | Increase `profit_transfer_fraction` or lower child's initial costs |

## Hyperparameter calibration

### Which parameters to tune

| Parameter | When to tune | Direction |
| --------- | ------------ | --------- |
| `heartbeat_interval_seconds` | Darwin is burning too much on LLM per heartbeat | Increase (fewer heartbeats = fewer LLM calls) |
| `min_dispatches_before_mutation` | Mutations fire too early (Darwin hasn't proven itself) | Increase |
| `min_recurring_failures_for_mutation` | Mutations fire on noise (variance, not real bugs) | Increase |
| `min_profit_for_reproduction_usdc` | Reproduction fires too easily (weak parents reproduce) | Increase |
| `profit_transfer_fraction` | Children are either too rich (parent starves) or too poor (child dies) | Adjust toward balance |
| `tier_coding_max_daily_calls` | Mutation proposals burning budget | Decrease |

### Calibrating from production data

The `tools/sim/calibration.py` module accepts real production observations and applies them to the simulation:

```python
from tools.sim.calibration import CalibrationDataset, SimCalibration

ds = CalibrationDataset.from_file("calibration.json")
# calibration.json contains measured values:
# {"points": [
#   {"metric": "compute_usd_per_day", "value": 8.5, "source": "billing:fleet", ...},
#   {"metric": "revenue_usd_per_effective_piece", "value": 0.35, "source": "audit:d-1", ...}
# ]}

calibration = SimCalibration(ds)
world = SimWorld(scenario)
calibration.apply(world)  # overrides world constants with real data
```

**Supported calibration metrics:**

| Metric | Source | What it affects |
| ------ | ------ | --------------- |
| `compute_usd_per_day` | Billing service average | Sim's daily LLM + infra cost |
| `revenue_usd_per_effective_piece` | Audit log (real affiliate payouts) | Sim's revenue per published piece |
| `captcha_cadence` | Escalation ticket frequency | Sim's captcha-every-N-dispatches |

### When to re-calibrate

- After every 7 days of Phase 1 production running.
- After every hyperparameter change.
- After every new strategy is added to Layer 0.
- When the sim's predictions diverge significantly from real Darwin behavior.

## The 90-day simulation as a tuning tool

### Scenarios

| Scenario | Purpose | Expected outcome |
| -------- | ------- | ---------------- |
| `default` | Standard economics ($80 start, normal revenue, occasional captchas) | Survives 90 days, reproduces at least once |
| `harsh` | Thin runway ($1.30), low revenue, frequent captchas | Dies gracefully (cleanly, with audit intact) |
| `captcha_storm` | High revenue but constant captchas | Survives via escalation (proves P4: escalate-don't-fail) |

### Running a tuning sweep

```bash
# Run all scenarios:
uv run python -m tools.sim.run_sim --days 90 --scenario default
uv run python -m tools.sim.run_sim --days 90 --scenario harsh
uv run python -m tools.sim.run_sim --days 90 --scenario captcha_storm

# With custom hyperparameters (modify in code temporarily or via a
# future --hyperparameters flag):
# Adjust min_dispatches_before_mutation, heartbeat_interval_seconds, etc.
# and re-run to see if the survival/reproduction outcome changes.
```

### Interpreting sim reports

The `SimReport` contains:

```python
SimReport(
    scenario="default",
    days=90,
    alive=True,           # survived the full run?
    final_balance_usdc=142.5,  # ending treasury
    content_pieces=45,
    published_pieces=38,
    reproductions=1,      # how many children spawned?
    fitness=0.12,         # final rolling fitness
    death_reason=None,
    invariants_ok=True,   # all 8 invariants held?
)
```

**What to look for:**

| Signal | Meaning | Action |
| ------ | ------- | ------ |
| `alive=False` + `death_reason="treasury_exhausted"` | Burning money faster than earning | Increase heartbeat interval, reduce coding-tier calls |
| `reproductions=0` after 90 days | Never reached fitness + surplus gate | Check if the default scenario's revenue is too low relative to costs |
| `fitness` declining over time | Something is degrading the Darwin's performance | Review the timeline for mutation events that reduced fitness |
| `invariants_ok=False` | A fundamental contract is broken | This is a bug in the genome, not a tuning problem |

## Generational health indicators

Across a lineage (parent → child → grandchild):

| Indicator | Healthy | Concerning |
| --------- | ------- | ---------- |
| Child's 7-day fitness ≥ parent's 7-day fitness at same age | Inherited lessons are helping | Lessons are noise, not signal |
| Dead ends grow across generations | The lineage is learning what to avoid | If dead ends grow but fitness doesn't improve, the lessons are too conservative |
| Generation depth increases steadily | The lineage is viable and reproducing | If stuck at gen 1 for weeks, reproduction conditions are too strict |
| Child survival rate ≥ 50% past 7 days | Budget transfer is adequate | Transfer fraction too low or starting costs too high |

## Quick reference: tuning for common problems

| Problem | First thing to check | Likely fix |
| ------- | -------------------- | ---------- |
| Darwin never mutates | `recurring_failures` dict | Lower `min_recurring_failures_for_mutation` if failures are genuinely recurring |
| Darwin mutates too much | Mutation archive entries per day | Raise `min_dispatches_before_mutation` |
| Darwin never reproduces | Fitness gauge + balance | Lower `min_profit_for_reproduction_usdc` or improve the strategy |
| Child always dies | Child's opening balance | Raise `profit_transfer_fraction` |
| Fitness oscillates wildly | Policy violations counter | Investigate which actions are triggering violations |
| Revenue positive but fitness negative | `policy_violations > 0` | Fix the violation source (δ=10 penalty is massive) |
| Sim passes but real Darwin struggles | Calibration staleness | Re-calibrate with latest production metrics |
