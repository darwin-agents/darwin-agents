# darwin_dna — Layer 0, the founding genome

This repository implements **Layer 0** of Darwin: the signed, immutable Python
package (`darwin_dna`) that every Darwin organism inherits as its base DNA
layer. The design lives in [`docs/`](docs/); the authoritative build plan is
[`docs/engineering/18-layer-zero-build-plan.md`](docs/engineering/18-layer-zero-build-plan.md).

Layer 0 contains the *rules of the game and the machinery to play it* — never
the business logic. Business logic is the mutable soma's job and is out of scope
for this package.

## What's here

| Path | What it is |
|---|---|
| `src/darwin_dna/` | The signed Layer 0 genome. ≤ 5k lines of meaningful Python, curated-allowlist deps only. |
| `src/darwin_fakes/` | In-memory fakes for the external world (LLM gateway, sandbox, wallet, platform, vault). Not part of the genome. |
| `tools/sim/` | The simulated world. Runs a Darwin for N simulated days and asserts invariants. Not part of the genome. |
| `tests/` | Paranoid suites for the four hot paths + integration + simulation. |

## The four hot paths (paranoid)

`signature` + `manifest` (trust root), `kill_switch` (off switch),
`audit_logger` (write-before-act truth), `treasury_policy` (wallet bouncer).
A malicious soma is assumed in their tests.

## Quickstart

```bash
uv sync --extra dev
uv run pytest -q                       # unit + property + integration + sim
uv run python -m tools.sim.run_sim --days 90   # watch a Darwin live for 90 simulated days
```

## Boot guarantee

Layer 0 boots a Darwin into a *working* state on first run — naive and
unprofitable is acceptable, non-functional is not. The loop runs, neurons
dispatch, the ledger reconciles, the kill switch halts, the audit chain is
unbroken. Evolution improves everything above that floor; nothing falls below it.
