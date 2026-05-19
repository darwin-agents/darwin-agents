"""The founding orchestrator graph (docs/engineering/18, "the orchestrator
skeleton").

A Darwin *boots* from this graph; its soma copy may then mutate it freely. The
graph is checkpointed at every node boundary so the Darwin can survive a
sandbox replacement at any point, and the kill switch is polled at every node
boundary so a kill takes effect before the next side-effecting action (P8).

Engine: the node functions are deliberately framework-agnostic
``(state, ctx) -> state``. In production they are wired into LangGraph (which
is on the Layer 0 allowlist). The genome must also boot and the simulation
must run offline and deterministically, so an in-process runner with
*identical* checkpoint + kill-poll semantics is the default; ``engine="langgraph"``
wires the very same node functions into a real ``StateGraph``.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from ..machinery.kill_switch import KillSwitch
from .nodes import (
    apply_mutation,
    check_death,
    check_reproduction,
    dispatch,
    evaluate_mutation,
    perceive,
    plan,
    propose_mutation,
    reflect,
)
from .state import OrchestratorState

END = "__end__"


@runtime_checkable
class CheckpointStore(Protocol):
    """Durable checkpoint of working memory at each node boundary."""

    def save(self, darwin_id: str, node: str, state: dict[str, Any]) -> None: ...

    def load(self, darwin_id: str) -> dict[str, Any] | None: ...


@dataclass
class OrchestratorContext:
    """Everything the nodes need, wired by the evolution loop. The nodes
    never reach outside this bundle — that is what keeps them testable and
    keeps the soma from acquiring capabilities it was not granted."""

    darwin_id: str
    kill_switch: KillSwitch
    audit: Any  # AuditLogger
    llm: Any  # LLMRouter
    catalog: dict[str, Any]  # name -> Neuron
    strategy: Any  # Strategy
    wallet: Any  # WalletPort
    layer_stack: Any  # LayerStack
    hp: Any  # Hyperparameters
    escalation: Any  # EscalationInbox: .post(ticket_dict)
    checkpoint: CheckpointStore
    build_neuron_context: Callable[..., Any]
    clock: Callable[[], float] = time.time


# The graph topology, exactly the doc's diagram. Each entry:
# node name -> (node fn, router(state)->next node name).
_NODES: dict[str, Callable[[OrchestratorState, OrchestratorContext], OrchestratorState]] = {
    "check_death": check_death.run,
    "perceive": perceive.run,
    "plan": plan.run,
    "dispatch": dispatch.run,
    "reflect": reflect.run,
    "propose_mutation": propose_mutation.run,
    "evaluate_mutation": evaluate_mutation.run,
    "apply_mutation": apply_mutation.run,
    "check_reproduction": check_reproduction.run,
}


def _next(state: OrchestratorState) -> str:
    """Conditional edge: the node sets ``state.route``; we honour it. An
    unset/unknown route is a graph bug — fail loud, never wander."""
    nxt = state.route
    if nxt != END and nxt not in _NODES:
        raise RuntimeError(f"graph: node set unknown route {nxt!r}")
    return nxt


class _InternalRunner:
    """Checkpointed, kill-polled, single-heartbeat executor.

    One ``invoke`` runs the decision flow once: check_death → perceive → plan
    → (dispatch|escalate-handled-in-node|propose_mutation|sleep) → reflect →
    check_reproduction → END. The outer evolution loop calls this each
    heartbeat and owns the sleep + the death sequence.
    """

    def __init__(self, ctx: OrchestratorContext):
        self._ctx = ctx

    async def invoke(self, state: OrchestratorState) -> OrchestratorState:
        node = "check_death"
        # Kill switch is polled at the *top* of the heartbeat and again at
        # every node boundary below. Raising KillSignal is intentional: the
        # evolution loop catches it and runs the halt sequence.
        self._ctx.kill_switch.check()
        while node != END:
            fn = _NODES[node]
            state.note(node)
            result = fn(state, self._ctx)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                state = result
            # checkpoint AFTER the node commits its effect, BEFORE we move on
            self._ctx.checkpoint.save(
                self._ctx.darwin_id, node, state.model_dump()
            )
            self._ctx.kill_switch.check()  # boundary poll
            node = _next(state)
        return state


def build_graph(ctx: OrchestratorContext, engine: str = "internal"):
    """Return a single-heartbeat runnable with an async ``invoke(state)``.

    ``engine="internal"`` (default): the deterministic offline runner above.
    ``engine="langgraph"``: the same node functions wired into a real
    ``langgraph.StateGraph`` with an in-memory checkpointer. Falls back to
    internal if langgraph is not installed — the genome boots either way.
    """
    if engine == "langgraph":
        try:
            return _build_langgraph(ctx)
        except Exception:
            # P9 / boot guarantee: never fail to boot over an optional
            # framework. The internal runner is behaviourally identical.
            return _InternalRunner(ctx)
    return _InternalRunner(ctx)


def _build_langgraph(ctx: OrchestratorContext):  # pragma: no cover - optional path
    """Wire the identical node functions into LangGraph. Kept thin: the
    behaviour lives in the node modules, not the framework."""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END as LG_END
    from langgraph.graph import StateGraph

    sg = StateGraph(dict)

    def wrap(name, fn):
        async def _node(d: dict):
            st = OrchestratorState.model_validate(d)
            st.note(name)
            out = fn(st, ctx)
            if inspect.isawaitable(out):
                out = await out
            st = out if out is not None else st
            ctx.checkpoint.save(ctx.darwin_id, name, st.model_dump())
            ctx.kill_switch.check()
            return st.model_dump()

        return _node

    for n, fn in _NODES.items():
        sg.add_node(n, wrap(n, fn))
    sg.set_entry_point("check_death")

    # The node sets state.route; map it to the next node or to LangGraph END.
    edge_map = {k: k for k in _NODES}
    edge_map[END] = LG_END
    for n in _NODES:
        sg.add_conditional_edges(
            n,
            lambda d: OrchestratorState.model_validate(d).route,
            edge_map,
        )

    compiled = sg.compile(checkpointer=MemorySaver())

    class _LGRunner:
        """One ``invoke`` == one heartbeat. Each heartbeat gets its OWN
        checkpoint thread (``darwin_id:tick``): intra-heartbeat node
        checkpointing is preserved, but the next heartbeat starts fresh at
        check_death instead of resuming a thread that already reached END."""

        async def invoke(self, state: OrchestratorState) -> OrchestratorState:
            ctx.kill_switch.check()
            cfg = {
                "configurable": {"thread_id": f"{ctx.darwin_id}:{state.tick}"}
            }
            out = await compiled.ainvoke(state.model_dump(), cfg)
            return OrchestratorState.model_validate(out)

    return _LGRunner()
