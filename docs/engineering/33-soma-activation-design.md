# Soma activation — making self-modification actually run

## Status

**Option A + B implemented (2026-06-04).** `darwin_platform/evolution/soma_loader.py`
(`SomaLoader`) loads the validated soma `strategies/` overlay; the runtime binds
it via the `platform.strategy` injection seam (`darwin_dna/evolution/loop.py`),
wired into both the in-process supervisor spawn/child-boot paths and the k8s pod
orchestrator. Gated on `--soma-root`, Layer-0 immutable, fail-closed to
`founding_strategy()`. **Option B (hot-swap) is now also built:** `apply_mutation`
sets `state.soma_reload_pending` when a real patch lands; the platform drive loop
consumes it at the quiescent boundary and calls `runtime.rebind_strategy()` /
`runtime.merge_neurons()` so a promoted rewrite takes effect THIS life. Three
further pieces landed alongside: a **soma-authored neuron overlay**
(`SomaLoader.load_neurons()` under the Path-C tag allowlist + contract check,
merged via `platform.neuron_overlay` / `runtime.merge_neurons`), and a
**real-candidate eval** (`eval_harness.evaluate_strategy_candidate` runs the real
strategy over the frozen tape; `PatchStagingService` applies a strategy patch to
a temp soma, loads it, and scores it instead of the `BehaviorDelta` proxy).
Option C (offspring inheritance) remains rejected (contradicts doc 06).

Originally written 2026-06-04 after a code audit found that promoted
self-modifications were written to disk but **never executed by anything** — the
Phase-1 thesis (doc 21) was, as built, unrealized. This doc records both the
analysis and the chosen path.

## The problem in one sentence

A Darwin's `apply_mutation` node writes a validated patch to its soma tree on
disk (`<soma_root>/<id>/soma/strategies/*.py`), but no code path ever loads or
executes those files — not the running process, not a restart, not a child — so
self-modification currently changes **forensics and rollback state only, never
behavior**.

## Evidence (as-built)

- **The strategy is the static package, bound once at boot.**
  `darwin_dna/evolution/loop.py:225` — `self.strategy = founding_strategy()`
  instantiates `ContentAffiliateStrategy` from the installed
  `darwin_dna.strategies.content_affiliate` module. It is never reloaded or
  swapped for the life of the process.
- **"DNA layers" are data, not code.** `darwin_dna/layers/_stack.py` parses each
  `layer_blob` as a JSON `list[Lesson]`. Layers carry *lessons* (structured
  business rules), not executable Python. The genome's executable code is the
  shipped package image, identical in every pod.
- **No dynamic code loading exists.** Zero occurrences of `importlib`, `exec(`,
  `compile(`, `SourceFileLoader`, `spec_from_file_location`, `runpy`, or
  `sys.path` mutation anywhere in `src/darwin_dna/`.
- **`apply_mutation`'s only behavioral effects** (`orchestrator_skeleton/nodes/
  apply_mutation.py`) are: clear the recurring-failure counter, write an archive
  entry, optionally admit a Path-C neuron to the in-memory catalog, and call
  `ctx.soma_merge.merge()` — which writes the diff to disk. Nothing re-imports
  the patched file.
- **Reproduction does not pick it up either.** `evolution/reproduction.py`
  builds the child from `inherited_layer_blobs` (lessons JSON) + freshly
  distilled lessons from semantic memory. The parent's soma patch files are not
  read. This is consistent with doc 06 §"Why the child doesn't inherit the
  soma" — a *deliberate* design choice that lessons (legible) propagate, not
  mutable code (opaque).

## Intent vs. reality

The docs assume the soma **is** the running code of the *current* Darwin:

- doc 03: "The soma is the mutable body of the Darwin… `strategies/` (mutable,
  learned)."
- doc 21: "a Darwin rewrites its own soma based on observed failures, the
  rewrite measurably improves"; "the supervisor applies the diff to the live
  soma at the next safe checkpoint"; "where the live soma runs".
- doc 06: child gets "a fresh soma (no inherited mutable code — that has to be
  reconstructed by the child)"; runtime soma changes are "lost. This is by
  design."

So the documented model is coherent: **the running Darwin executes its own
soma; offspring deliberately do *not* inherit it.** The gap is purely that the
"the running Darwin executes its own soma" half was never built — `apply_mutation`
was wired to *write* the soma (gap #16) but nothing was wired to *run* it.

A second consequence: the Phase-1 eval harness (doc 21) is supposed to stage a
candidate soma in a sandbox and measure it, but with no live soma it can only
run a `BehaviorDelta` simulation. Activating the soma also unblocks *real*
candidate evaluation.

## What this is NOT

This is **not** "Layer-0 should be able to rewrite itself." P1 (Layer-0
read-only for self-reflection) stays absolute. The genome core — kill switch,
patch validator, audit chain, ledger, constitution, the loop itself — remains
the static signed package and must never be loadable from the soma. The patch
validator already enforces this on the *write* side: only `soma/strategies/`
and a small allowlist are writable; DNA / validator / kill-switch paths are
refused. The loader must enforce the same boundary on the *read* side.

## Options

### Option A — Boot-time soma loader (recommended)

At genome boot, after the trust root and constitution checks, load the
**validated** soma `strategies/` (and later `neurons/`) as an overlay over the
package defaults, then bind the strategy from the overlay instead of
`founding_strategy()`.

- **Mechanics:** a new `SomaLoader` (platform side, like `SomaMerger`) resolves
  `<soma_root>/<id>/soma/strategies/*.py`, re-runs `validate_patch`-equivalent
  path/AST checks, imports each module under a sandboxed module namespace
  (`importlib.util.spec_from_file_location` into a private package), and exposes
  the resulting strategy classes. `boot()` prefers an overlay strategy when
  present, else `founding_strategy()`.
- **When it takes effect:** next process start. With the soma now PVC-persisted,
  a pod restart / supervisor re-adoption / respawn picks up the accumulated
  evolution. This matches doc 21's "applies the diff to the live soma at the
  next safe checkpoint" if we treat a boot as the safe checkpoint.
- **Pros:** simplest; one well-defined load point; no mid-flight code swapping
  (no torn state); a restart is already a natural quiescent boundary; trivially
  auditable (load once, log the digest).
- **Cons:** a landed mutation doesn't change behavior until the next boot. For a
  long-lived pod that's a real lag — acceptable for v1 (mutations are rare and
  the eval harness gates them), revisited by Option C.

### Option B — Hot-swap after a landed mutation

After `apply_mutation` lands a patch, reload the affected module and rebind the
strategy/catalog **through the context** (the `loop.py:284` comment describes
exactly this intent: "the soma swaps strategy/catalog through the context").

- **Pros:** the rewrite improves the *current* Darwin immediately — the literal
  reading of the Phase-1 thesis.
- **Cons:** reloading code into a live, partially-executed graph is the riskiest
  path — torn state between heartbeats, half-applied catalogs, harder rollback,
  and the biggest attack surface (a self-written module executes in-process the
  moment it lands). Needs a quiescent-point barrier (between heartbeats only)
  and a tested rollback-on-import-error. Strictly more complex than A and shares
  all of A's machinery. **Defer to a phase-2 follow-on built on top of A.**

### Option C — Distill soma into offspring (rejected)

Fold promoted soma patches into the child's inherited DNA at reproduction.

- **Rejected:** directly contradicts doc 06 §"Why the child doesn't inherit the
  soma," a deliberate, documented decision that *lessons* (legible) propagate
  and *mutable code* (opaque) does not. Pursuing this would be a design reversal,
  not a gap-fill. If revisited, it belongs in doc 06 as an explicit amendment,
  not here.

## Safety model (applies to A and B)

1. **Layer-0 stays static.** Only `soma/strategies/` (+ a future `soma/neurons/`
   allowlist) is loadable. The loader refuses anything the patch validator would
   refuse to write — same allowlist, same path-sandbox (`_within_root`), same
   AST guard. Never DNA, validator, kill switch, ledger, constitution, loop.
2. **Only validated, promoted soma loads.** A file only reaches the soma tree by
   passing `validate_patch` + the eval harness `promote` verdict + `SomaMerger`.
   The loader is the read-side twin of that write-side gate, re-validating
   independently (never trust prior state on disk — a tampered PVC must fail
   closed to `founding_strategy()`).
3. **The blast radius is unchanged.** Loaded soma runs inside the *same*
   per-Darwin kata/gVisor microVM, behind the *same* egress proxy, under the
   *same* kill switch, treasury caps, and audit chain. A malicious soma cannot
   exceed the sandbox the Darwin already has; it can only make *this* Darwin
   behave worse — which the treasury death condition already selects against.
4. **Opt-in.** Gated behind the existing `--soma-root`. Unset → no loader, no
   merger, `founding_strategy()` only (today's behavior). No production behavior
   change until an operator opts in.
5. **Fail closed + audited.** Any load/validate/import error logs an audit event
   and falls back to the package default; it never crashes boot and never
   half-loads.

## Recommendation

Build **Option A (boot-time loader)**, gated on `--soma-root`, Layer-0 immutable,
re-validating independently and failing closed to `founding_strategy()`. It is
the smallest change that makes self-modification real, it rides the
just-landed PVC persistence, and a process boot is the safest possible load
point. Treat **Option B (hot-swap)** as a later increment layered on A's
machinery, and keep **Option C** out unless doc 06 is deliberately amended.

This also closes the secondary gap: once the soma is the live code, the Phase-1
eval harness can stage and measure the *real* candidate soma instead of a
`BehaviorDelta` simulation.

## Implementation sketch (Option A)

- `darwin_platform/evolution/soma_loader.py` — `SomaLoader(soma_root)`:
  `load_strategies()` → validated, imported strategy classes (private module
  namespace); independent path + AST re-validation; returns `{}` on any failure.
- `darwin_dna/evolution/loop.py::boot` — accept an optional strategy provider
  from the platform; prefer an overlay strategy, else `founding_strategy()`.
  (Keep `darwin_dna` ignorant of the filesystem; the platform passes the loaded
  classes in, mirroring how `ctx.soma_merge` is injected.)
- `runtime/supervisor.py` + `runtime/orchestrator.py` — when `soma_root` is set,
  build the loader and pass its strategies into `boot` (both in-process and pod
  paths; the pod already has `--soma-root` + the PVC from this slice).
- Audit: `soma.loaded` with the per-file digests actually executed (ties the
  running code to the forensic snapshot's `soma_digest`).

## Testing

- Loader: loads a valid `soma/strategies/x.py`; refuses a DNA/validator path;
  refuses a path escaping the soma root; fails closed (returns `{}`) on a syntax
  error / tampered file; AST guard rejects disallowed imports.
- boot: with an overlay present the runtime binds the overlay strategy; without
  one it binds `founding_strategy()` (parity).
- End-to-end: merge a patch via `SomaMerger`, restart, assert the new boot runs
  the patched behavior + audits `soma.loaded` with the matching digest.
- Safety: a soma file that tries to import the kill switch / mutate Layer-0 is
  refused; the Darwin boots on the package default.

## Open questions

- **`neurons/` overlay:** DONE — `SomaLoader.load_neurons()` loads
  `soma/neurons/*.py` under the Path-C tag allowlist + the one-money-door
  contract check, and the patch validator now permits *writing* a whole neuron
  file (the write is gated on paths/anchors; the run is gated on the Neuron
  protocol + tags), so self-authored neurons are symmetric with strategies.
  Still open: the genome's `propose_mutation` does not yet *generate*
  neuron-file patches (it emits strategy patches + Path-C structured proposals),
  so the write path is exercised by operators/tests today, not autonomously.
- **Hot-swap barrier (Option B):** DONE — `apply_mutation` raises
  `state.soma_reload_pending`; the platform drive loop reloads + rebinds at the
  between-heartbeats quiescent boundary.
- **Eval-harness rewire:** DONE — `evaluate_strategy_candidate` runs the real
  candidate; `StrategyObservation.recurring_failures` exposes the live
  failure signal so the candidate's *failure-avoidance* is measured (not just
  metabolism). Still open: the strategy economic model in the harness
  (dispatch = earn/cost) is a proxy, not the full neuron economy.
