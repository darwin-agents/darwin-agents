# Evolution and reproduction

This is where the metaphor and the math have to actually meet. Without a working reproduction event, Darwin is just a self-modifying agent with a wallet. With it, we have an open-ended evolutionary system.

## The DGM heritage, and where we diverge

The Darwin Gödel Machine (Sakana AI / UBC, 2025) established the operational core we reuse:

1. Maintain an **archive** of agent variants.
2. **Sample** a parent from the archive (biased toward useful diversity, not just top performers).
3. The parent proposes a **self-modification** using a foundation model.
4. Run the child against an **evaluation harness**.
5. If the child clears the bar, add it to the archive.
6. Loop.

DGM optimized SWE-bench resolve rate. Darwin optimizes economic survival. That changes three things:

- **The fitness function is real-world**, not a benchmark. Profit-since-last-reproduction, weighted by lineage health (penalize behaviors that are profitable but illegal, abusive, or that create unrecoverable platform risk).
- **The "archive" is a lineage tree, not a flat population** — at least for the v1 platform. Each Darwin is "owned" by a single user, and reproduction happens in-tree. We may add cross-tree borrowing later (see open questions).
- **Death is a thing.** DGM agents don't die; bad variants just don't get sampled. Darwins die because their treasury hits zero. This produces stronger selection pressure and rougher dynamics.

## What counts as fitness

Fitness is computed over a rolling window (default 7 days, parameterizable per lineage). It's a weighted score, not pure profit, to avoid pathological optima:

```
fitness = α × normalized_profit
        + β × strategy_diversity_bonus
        + γ × escalation_quality_score
        - δ × policy_violation_penalty
```

- **normalized_profit** — USDC profit in the window, normalized by starting capital.
- **strategy_diversity_bonus** — small reward for sampling different business archetypes. Prevents premature lock-in to one strategy.
- **escalation_quality_score** — rewards escalations the owner approved (Darwin asked good questions); penalizes ignored or rejected ones (Darwin pestered the owner).
- **policy_violation_penalty** — refused payments, blocked actions, anomaly triggers.

There is deliberately **no volatility term**. Ruinous variance is already selected against by the hardest mechanism in the system — the treasury hitting zero and the Darwin dying. Penalizing variance *again* in the score would double-count the same risk and bias the whole ecosystem toward mediocrity, suppressing high-variance, high-expected-value lineages (trading among them) that are entirely legitimate. The narrower worry — a lineage reproducing off a lucky spike right before the check — is handled by *measurement*, not by an anti-volatility penalty: the rolling fitness window, the sustained `MIN_PROFIT_FOR_REPRODUCTION_USDC` threshold, and profit read from the on-chain-reconciled ledger. Skill survives a window; luck usually doesn't. The agent is left as autonomous as the death condition allows — which is the whole point.

The weights (α, β, γ, δ) are themselves part of the layer-0 DNA and intentionally not evolvable. Letting a Darwin reweight its own fitness function is the obvious wireheading path; we don't allow it.

## The reproduction event

When fitness within the window exceeds the reproduction threshold AND the treasury has enough surplus to fund a child, the orchestrator triggers reproduction:

```
1. PAUSE. Halt the soma at the next safe checkpoint.
2. DISTILL. Run the lesson-distillation pass: 
     - Inspect recent successful strategies and decisions.
     - Inspect failed mutations the archive remembers.
     - Generate a candidate new DNA layer (mostly structured lessons, see 02-dna-core.md).
3. VALIDATE. The candidate layer is run through the DNA validator:
     - Syntactic checks (it loads, signatures verify, no forbidden constructs).
     - Semantic checks against the constitution (no rule weakening).
     - Empirical check: a sandbox-spawned child with this DNA runs the standard test battery.
4. SIGN. If valid, the platform signs the new layer.
5. SPAWN. The platform provisions a new Darwin instance with:
     - The parent's DNA + the new sealed layer.
     - A starting budget transferred from the parent's treasury (typically 50% of surplus above the parent's minimum-viable, parameterizable).
     - A fresh soma (no inherited mutable code — that has to be reconstructed by the child).
     - A new per-Darwin wallet, vault, and event log.
6. REGISTER. The lineage tree gets a new edge. The child's identity card lists parent_id and generation.
7. RESUME. The parent continues running. (We do not kill on reproduction. A successful parent is valuable.)
```

The child is *not* a copy of the parent's running state. It boots fresh from its DNA. Everything the parent learned is in the new sealed layer; everything it figured out only at runtime in its soma is lost. This is by design — we want lessons that survive to be *legible* lessons, not opaque bag-of-weights memories.

## Why the child doesn't inherit the soma

This is the most controversial decision in this section. Arguments for inheriting the soma:

- More information transmits per generation.
- Children start "smarter" — their orchestrator has already been tuned.
- Mirrors biological inheritance more closely (zygotes inherit cellular machinery too).

Arguments against (the position we hold):

- **Legibility.** A lesson that has to be encoded as a structured DNA entry, not a code patch, is reviewable by humans and by future generations. Inheriting opaque soma changes makes the lineage's behavior un-explainable over many generations.
- **Audit.** Regulators, the platform, and the user need to be able to say "this Darwin behaves this way because of *this specific lesson it inherited from generation N*". A 5000-line evolved soma defeats this.
- **Convergence on local optima.** If children start with the parent's soma, they explore tiny perturbations around it. If they start fresh and rediscover from lessons, they can find different local optima.
- **Failure isolation.** A bug in the parent's soma doesn't propagate to the child if the child reconstructs.

The cost is real (each new child has to re-learn the orchestrator details), but the founding soma in layer-0 is shared across all lineages, so the floor isn't low. We can revisit if lineages turn out to plateau too quickly.

## The archive

Beyond direct parent-child inheritance, each lineage maintains an archive of past variants:

- **Promoted ancestors** — every Darwin in the lineage's direct ancestry.
- **Sibling archive** — past failed mutation attempts kept for "negative lessons" (so the lineage doesn't re-try the same dead end).
- **Cross-pollination archive (phase 3, optional)** — soma snippets from successful Darwins in *other* lineages, sampled by the platform with user consent.

Cross-pollination is the most powerful and the most fraught option. It breaks the "each lineage is isolated" property but unlocks much faster collective progress. We don't enable it in v1; we design for it being possible later (see [`../overview/06-roadmap-and-phases.md`](../overview/06-roadmap-and-phases.md)).

## Generation depth limits

Without a cap, a successful lineage's DNA grows by one layer per reproduction and could reach hundreds of layers. Two problems:

- DNA load time grows linearly with layer count.
- Lessons from many generations back are often stale (the world changed) but still get consulted.

Default policy: **soft cap at generation 20.** When a Darwin would produce generation 21+, the reproduction event includes a *layer compaction* step where the platform's signing service consolidates the oldest several layers into a single distilled layer using a coding LLM. The original layers remain in the platform's archive for forensics; the child carries the compacted form.

This is unsatisfying — it means humans (via the platform) curate the deep ancestry — but the alternative is the lineage being read-locked to its own history. We will revisit.

## Open questions for this section

These are flagged but not yet decided. Treat them as research items:

- **Cross-lineage learning.** Should successful Darwins anonymously contribute "lessons" to a shared platform-curated pool that newly-spawned Darwins can read? Pro: faster ecosystem progress. Con: violates lineage isolation, raises IP and privacy questions (whose lesson is it?). *As built (2026-10):* the mechanism exists as `CrossPollinationArchive` (`src/darwin_platform/tenancy/cross_pollination.py`) — platform-signed, size- and rate-bounded, topic-scoped deposits at reproduction and injection at child boot (red-team: `tests/redteam/test_cross_pollination_poison.py`). It is off unless an archive is passed to `Supervisor(cross_pollination_archive=...)`; `darwin-platform serve` does not enable it, so the policy question remains open.
- **Owner-mediated mutation.** Should the owner be able to *suggest* lessons or mutations? Pro: humans see things Darwins don't. Con: blurs the line between an autonomous agent and a meatspace remote control.
- **Inheritance of the wallet on parent death.** If a parent dies after producing children, do the children inherit any remaining funds? Currently: no. The user gets the residual. But a "dynasty mode" where wealth concentrates over generations is interesting and probably risky.
- **Sexual reproduction.** Can two Darwins (same owner, or two consenting owners?) reproduce together, combining their DNAs? Mostly a thought experiment but the recombination dynamics could be powerful. Not in v1.
- **Variation injection.** Should the platform occasionally force a small random mutation into a new layer at reproduction, to prevent lineages from converging on a single strategy too aggressively? Probably yes. Magnitude TBD.
