# Orchestration — LangGraph

## Decision

Use **LangGraph** as the orchestrator framework for the Darwin's main loop, the self-modification engine, and individual neurons' internal control flow where useful.

## Why LangGraph (2026 landscape)

We evaluated five frameworks against Darwin's specific needs:

| Framework | Orchestration model | State persistence | Multi-LLM | HITL | Verdict |
|---|---|---|---|---|---|
| **LangGraph** | Directed graph + conditional edges | Built-in checkpointing, time travel | Fully agnostic | First-class interrupts | **Selected.** |
| CrewAI | Role-based "crews" | Task outputs passed sequentially | Agnostic | Limited | Rejected — too opinionated for dynamic re-planning. |
| AutoGen / AG2 | Conversational GroupChat | In-memory by default | Agnostic | Limited | Rejected — conversation pattern doesn't map well to Darwin's "agent that runs forever". |
| OpenAI Agents SDK | Explicit handoffs | Ephemeral context | OpenAI only | Minimal | Rejected — provider lock-in violates P6. |
| Claude Agent SDK | Tool-use chain + sub-agents | Via MCP servers | Claude only | Hooks | Rejected — provider lock-in violates P6. |

LangGraph's winning attributes against Darwin's specific requirements:

- **Durable execution.** A Darwin runs for weeks or months. The orchestrator must survive process restarts, sandbox replacements, and host failures without losing in-flight work. LangGraph's Postgres-backed checkpoint store handles this.
- **Time-travel debugging.** When a Darwin makes a bad decision, we want to replay from any checkpoint. LangGraph supports it natively.
- **Human-in-the-loop as a primitive.** An escalation is a graph `interrupt()` that pauses until a resume token arrives. We don't have to invent escalation control-flow on top of a framework that wasn't built for it.
- **Branching + concurrent execution.** A Darwin sometimes runs multiple strategies in parallel (e.g. two ad campaigns) and joins results. LangGraph models this as a graph fan-out + reducer.
- **Maturity in stateful, audited workflows.** This is exactly the deployment shape LangGraph optimized for through 2025–2026.

## How we use it

The main orchestrator is a single LangGraph graph stored under `soma/orchestrator/graph.py`. Nodes:

- `perceive` — pulls treasury state, inbox, environment signals into the shared state
- `plan` — calls a reasoning-tier LLM with state + DNA lessons, decides next action
- `dispatch_neuron` — sends a task to a neuron, awaits result (with interrupt for escalations)
- `reflect` — updates memory and strategy stats post-action
- `propose_mutation` — when the self-mod engine has a candidate patch
- `evaluate_mutation` — staging-sandbox validation of a candidate patch
- `apply_mutation` — promote validated patch to live soma (with the orchestrator paused at a safe checkpoint)
- `check_death` — treasury and policy checks; transition to `death_sequence` if needed
- `check_reproduction` — fitness check; transition to `reproduce` if appropriate

Edges are conditional. The state object carries `current_strategy`, `pending_escalations`, `treasury_snapshot`, `recent_decisions`, `mutation_candidates`, `kill_flag`. Reducers merge concurrent updates predictably.

Each neuron, internally, also uses a small LangGraph (or a plain coroutine if the neuron is trivially linear). The orchestrator's `dispatch` node calls the neuron in-process inside the orchestrator (pod or asyncio task); code a neuron wants executed goes to the E2B sandbox it is handed. The neuron's internal graph is its own concern.

## Persistence layout

```
postgres (per-Darwin schema):
  langgraph_checkpoints              # the graph's state at each step
  langgraph_writes                   # pending writes
  langgraph_threads                  # one "thread" per long-running Darwin instance
  langgraph_interrupts               # paused-on-escalation tokens
```

Checkpoints are written before every state-mutating node returns, which gives us the audit log's "before any side effect" property when combined with our outbox pattern.

> **As built (2026-10):** the LangGraph Postgres saver is not used. The node functions are framework-agnostic; an in-process runner with the same checkpoint and kill-poll semantics is the default engine and `engine="langgraph"` wires the same nodes into a real `StateGraph` (`src/darwin_dna/orchestrator_skeleton/graph.py`). Checkpoints go through the genome's own `CheckpointStore` (latest state per node boundary): the `darwin_checkpoints` table for in-process Darwins (`src/darwin_platform/runtime/persistence.py`) or `FileCheckpointStore` on the pod's `/data` volume under `--driver k8s` (`src/darwin_platform/runtime/audit_sink.py`). There are no `langgraph_*` tables.

## What goes where

- **DNA-side:** the `evolution_loop` is a thin Python module that loads the soma's LangGraph and runs it. The DNA does not contain the graph itself.
- **Soma-side:** the graph definition, the nodes' implementations, the strategies they call into.
- **Platform-side:** the LangGraph checkpoint store (Postgres). The platform owns the data but never reads the *meaning* of a Darwin's state — only the supervisor reads the checkpoint metadata (e.g. "is this Darwin paused on an escalation older than 24h?") for operational purposes.

## Versioning

LangGraph is moving fast. We pin to a specific minor version in the platform's base image and treat upgrades as a coordinated rollout: spin up new sandboxes against the new version, run our standard test battery, promote.

A Darwin can self-modify its graph definition but cannot upgrade LangGraph itself — that's a platform decision, not a per-Darwin one.

## Cost considerations

LangGraph itself adds essentially no cost. The expensive parts are:

- LLM calls inside the `plan` node and within neurons.
- The checkpoint storage in Postgres (we estimate ~5KB per heartbeat × 1 heartbeat/sec ≈ 13MB/day per Darwin — trivial).
- The microVM cost while the orchestrator is running.

We expect to spend much more on the foundation model than on the orchestrator's bookkeeping.
