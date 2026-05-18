# Soma and orchestrator

The soma is the mutable body of the Darwin. The orchestrator is its central nervous system — the LangGraph workflow that reads the DNA, picks strategies, dispatches neurons, and decides when to modify itself.

## Why LangGraph

We evaluated four agent frameworks for the orchestrator role: LangGraph, CrewAI, Microsoft Agent Framework, and the OpenAI Agents SDK. LangGraph wins for Darwin because:

- **Stateful by default.** LangGraph models the agent as a directed graph with persistent state and checkpoints. A Darwin needs to survive process restarts, sandbox replacements, and orchestrator self-modifications without losing where it was.
- **Durable execution.** Long-running tasks that pause for owner escalation (sometimes for hours) need to checkpoint and resume cleanly. LangGraph's persistence model handles this natively.
- **Human-in-the-loop primitives.** Interrupts and resume tokens are first-class; we don't have to invent escalation control-flow ourselves.
- **Model-agnostic.** No provider lock-in. Pairs cleanly with our LiteLLM gateway.
- **Production track record.** As of 2026 it's the most-deployed agent framework for stateful, auditable workloads — exactly Darwin's profile.

CrewAI would be faster to prototype but its role-based abstraction makes the kind of dynamic re-planning Darwin needs harder. The OpenAI SDK is provider-locked. Microsoft's framework is too enterprise-shaped.

See [`12-orchestration-langgraph.md`](12-orchestration-langgraph.md) for the full comparison.

## What's in the soma

Everything except the DNA. Concretely:

```
soma/
  orchestrator/             # LangGraph definition for THIS Darwin
    graph.py
    nodes/
      perceive.py           # read treasury, read inbox, read environment
      plan.py               # pick strategy and next action
      dispatch.py           # send work to neurons
      reflect.py            # post-action analysis
      mutate.py             # propose soma rewrites
    state.py                # the shared state object the graph carries
  strategies/               # named business strategies (mutable, learned)
    blog_seo.py
    affiliate_funnel.py
    api_reseller.py
    ...
  neurons/                  # the agent's local copies of neuron clients
    content_client.py
    browser_client.py
    accounts_client.py
    ...
  memory/                   # vector store and structured memory
    episodic.sqlite
    semantic.lancedb
  workspace/                # scratch space; experiments live here before promotion
```

The soma directory is mounted into the Darwin's sandbox as read-write. The DNA directory is mounted read-only.

## The main loop

In plain English, every "heartbeat" of a Darwin runs roughly:

1. **Check kill-switch.** If flipped, halt and write the final ledger entry.
2. **Check treasury.** If below minimum-viable balance and no revenue inbound, trigger death sequence.
3. **Perceive.** Pull current state: treasury balance, in-flight neuron tasks, owner messages, market signals the strategy cares about.
4. **Plan.** The orchestrator's plan node calls a reasoning-tier LLM with the current state + the relevant lessons from DNA. It outputs a next action: either dispatch a neuron, escalate to the owner, propose a soma mutation, run a fitness evaluation, or sleep.
5. **Dispatch.** If the action is a neuron task, send it. If it's an escalation, post to the platform inbox. If it's a mutation proposal, hand it to the self-modification engine.
6. **Reflect.** When the dispatched work completes, the reflect node updates memory and the strategy's local stats.
7. **Maybe reproduce.** If profit since last reproduction exceeds the reproduction threshold, trigger the reproduction event.
8. **Sleep until next heartbeat.** Heartbeat interval is itself a learned parameter — too fast and we burn tokens, too slow and we miss opportunities.

The loop is implemented in LangGraph and the graph itself is part of the soma — meaning the Darwin can, in principle, rewrite its own loop. (In practice, the founding loop is robust enough that most learned mutations target strategies and neuron usage, not the loop structure. But the option is there.)

## The self-modification engine

This is the part that makes Darwin a Darwin, not just an agent. The self-modification engine is a sub-graph that:

1. **Identifies an improvement candidate.** Triggered by the reflect node when a strategy or neuron is underperforming, when the same error class recurs, or on a scheduled cadence.
2. **Proposes a patch.** A coding-tier LLM (e.g. Claude Opus, GPT-5.5-Codex, routed via the platform LiteLLM gateway) is given the relevant source file, the context, and the failure pattern. It returns a patch — a unified diff.
3. **Stages the patch.** The patch is applied in a fresh sandbox (E2B microVM) containing a copy of the soma. The DNA layer is mounted read-only as in production.
4. **Evaluates the patch.** The staged sandbox runs against a frozen evaluation harness — a replay of recent real situations (or synthetic ones) with simulated revenue/cost streams. We measure: did the patched soma make better decisions? Did it stay under spend limits? Did it not break any audit invariants?
5. **Promotes or discards.** If the evaluation is positive, the patch is merged into the live soma (with the live Darwin briefly paused at a safe checkpoint). If negative, the patch is archived along with the failure mode.

The evaluation harness is itself part of the DNA — we don't want a Darwin grading its own self-modifications using rules it can rewrite. The harness lives in `darwin_dna/machinery/fitness/`.

This is directly modeled on the Darwin Gödel Machine's "propose + empirical validation + archive" loop. See [`../overview/03-prior-art.md`](../overview/03-prior-art.md).

## State, memory, and persistence

A Darwin has three kinds of memory:

- **Working memory.** The LangGraph state object — current plan, in-flight tasks, recent observations. Lives in Postgres (the LangGraph checkpoint store). Wiped at sandbox replacement; reconstructed from checkpoints.
- **Episodic memory.** Append-only log of past actions and outcomes, with embeddings. Lives in a per-Darwin LanceDB instance. Queried when the planner wants "have I seen this before?"
- **Semantic memory.** Distilled lessons — "platform X rate-limits between Tuesday 03:00–05:00 UTC", "affiliate ID Y converted at 4× the others in November". Lives in a small structured table inside the per-Darwin database. This is the staging ground for lessons; the most useful ones get promoted to a DNA layer at the next reproduction.

A neuron call is a read of episodic and semantic memory, a write to episodic, and optionally a write to semantic (if the reflect node decides it's a lesson).

## What "the soma can rewrite itself" actually allows

To make this concrete, here is what is and is not in-scope for soma self-modification:

**In scope (can be rewritten by the agent):**
- The contents of any file under `soma/strategies/`
- The contents of any file under `soma/neurons/*_client.py` (the client logic, not the neuron itself)
- Most files under `soma/orchestrator/nodes/`, with restrictions on the perceive node's safety checks
- Hyperparameters in `soma/config.py`

**Out of scope (rejected by the patch validator):**
- Any file under `darwin_dna/`
- The patch validator itself
- Files containing the kill-switch poll
- The audit logger calls
- The treasury policy enforcement points

The patch validator runs as part of the staging step and is itself in the DNA. A patch that attempts to touch out-of-scope files is rejected before it even reaches the evaluation harness.

## Open question: how aggressive should the founding soma be at self-modification?

There's a tension. Too cautious and we don't get the compounding benefits of self-improvement. Too aggressive and a young Darwin will spend its starting budget on coding-tier LLM calls trying to refactor itself before earning a cent.

Current leaning: the founding hyperparameters bias *strongly* toward earning first, mutating second. Self-modification is gated on (a) having made at least N successful neuron dispatches, and (b) having identified a specific, recurring failure mode. We can loosen this as we see how lineages evolve.
