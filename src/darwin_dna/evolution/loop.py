"""The Darwin's main loop entry point (docs/engineering/03, 18).

``boot`` is the single front door. It runs the trust root, asserts the
constitution is internally coherent, wires the genome machinery to the
platform-provided ports, and returns a ``DarwinRuntime`` the platform (or the
simulation) drives heartbeat by heartbeat.

The boot guarantee (docs/engineering/18): a naive, unprofitable Darwin is
acceptable; a non-functional one is not. After ``boot`` the loop runs, neurons
dispatch, the ledger reconciles, the kill switch halts, the audit chain stays
unbroken — on first boot, with no mutations applied.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Protocol, runtime_checkable

from ..constitution import assert_constitution_invariants
from ..hyperparameters import DEFAULT_HYPERPARAMETERS, Hyperparameters
from ..identity import Identity
from ..layers import LayerStack
from ..machinery.audit_logger import AuditLogger
from ..machinery.kill_switch import KillSignal, KillSwitch
from ..machinery.llm_router import LLMRouter
from ..manifest import Manifest
from ..neurons import NeuronContext, build_catalog
from ..orchestrator_skeleton import OrchestratorContext, OrchestratorState, build_graph
from ..signature import load_platform_public_key
from ..strategies import founding_strategy
from .reproduction import ChildSpawner, PlatformSigner, reproduce


@runtime_checkable
class EscalationInbox(Protocol):
    """The owner notification bus. The genome only ever posts to it."""

    def post(self, ticket: dict) -> None: ...


class Platform:
    """The bundle of platform-provided ports a Darwin is born wired to. Real
    impls are the SaaS control plane; the fakes are an in-memory stand-in.
    The genome treats every one of these as untrusted-but-authoritative."""

    def __init__(
        self,
        *,
        manifest: Manifest,
        layer_blobs: dict[int, bytes],
        layer_names: dict[int, str],
        kill_source,
        audit_sink,
        anchor_store,
        llm_transport,
        wallet,
        provision,
        vault,
        checkpoint,
        escalation: EscalationInbox,
        signer: PlatformSigner,
        spawner: ChildSpawner,
        browser=None,
    ):
        self.manifest = manifest
        self.layer_blobs = layer_blobs
        self.layer_names = layer_names
        self.kill_source = kill_source
        self.audit_sink = audit_sink
        self.anchor_store = anchor_store
        self.llm_transport = llm_transport
        self.wallet = wallet
        self.provision = provision
        self.vault = vault
        self.checkpoint = checkpoint
        self.escalation = escalation
        self.signer = signer
        self.spawner = spawner
        self.browser = browser


class DarwinRuntime:
    """A booted, verified Darwin. Drive it with ``run``."""

    def __init__(
        self,
        *,
        identity: Identity,
        platform: Platform,
        layer_stack: LayerStack,
        hp: Hyperparameters,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        engine: str = "internal",
    ):
        self.identity = identity
        self._p = platform
        self._stack = layer_stack
        self._hp = hp
        self._clock = clock
        self._sleep = sleep

        self.audit = AuditLogger(identity.darwin_id, platform.audit_sink)
        self.kill = KillSwitch(platform.kill_source)
        self.llm = LLMRouter(platform.llm_transport, hp, clock=clock)
        self.catalog = build_catalog(
            wallet=platform.wallet, provision=platform.provision
        )
        self.strategy = founding_strategy()
        self._anchored_day: str | None = None

        # Survive sandbox replacement (docs/engineering/03, 12): working
        # memory is wiped when the sandbox is replaced and reconstructed from
        # the last node-boundary checkpoint. A genesis Darwin (no checkpoint)
        # starts fresh. The audit chain + wallet are durable platform-side, so
        # a resumed Darwin continues the *same* life, it does not relive it.
        restored = None
        try:
            restored = platform.checkpoint.load(identity.darwin_id)
        except Exception:
            restored = None  # a broken checkpoint must not block boot
        if restored and isinstance(restored, dict) and restored.get("state"):
            self.state = OrchestratorState.model_validate(restored["state"])
            self.resumed = True
        else:
            self.state = OrchestratorState()
            self.resumed = False

        ctx = OrchestratorContext(
            darwin_id=identity.darwin_id,
            kill_switch=self.kill,
            audit=self.audit,
            llm=self.llm,
            catalog=self.catalog,
            strategy=self.strategy,
            wallet=platform.wallet,
            layer_stack=layer_stack,
            hp=hp,
            escalation=platform.escalation,
            checkpoint=platform.checkpoint,
            build_neuron_context=self._build_neuron_context,
            clock=clock,
        )
        # Kept for introspection/tests. The soma swaps strategy/catalog
        # *through the context*, never by rebinding runtime attributes (those
        # are not what the graph reads).
        self._ctx = ctx
        self._graph = build_graph(ctx, engine=engine)
        # Birth happens once per life. A resume is a continuation, not a new
        # birth — logging "birth" twice would corrupt the lineage's story.
        self.audit.log_event(
            "resumed" if self.resumed else "birth",
            {
                "darwin_id": identity.darwin_id,
                "generation": identity.generation,
                "l0_version": identity.l0_version,
                "parent_id": identity.parent_id,
                **({"resumed_at_tick": self.state.tick} if self.resumed else {}),
            },
        )

    # -- neuron context wiring --------------------------------------------
    def _build_neuron_context(self, neuron_name: str) -> NeuronContext:
        relevant = self._stack.find(scope=f"neuron:{neuron_name}")
        nctx = NeuronContext(
            darwin_id=self.identity.darwin_id,
            llm=self.llm,
            audit=self.audit,
            vault=self._p.vault,
            lessons=relevant,
            now=self._clock,
        )
        # The browser substrate is a capability, granted only to the neuron
        # that needs it.
        if neuron_name == "browser.operator" and self._p.browser is not None:
            nctx.browser = self._p.browser
        return nctx

    # -- the loop ----------------------------------------------------------
    async def run(
        self,
        *,
        max_heartbeats: int | None = None,
        until: Callable[[], bool] | None = None,
    ) -> OrchestratorState:
        beats = 0
        try:
            while self.state.alive:
                if max_heartbeats is not None and beats >= max_heartbeats:
                    break
                if until is not None and until():
                    break

                self.state.days_elapsed = (
                    self._clock() - self.identity.birth_ts
                ) / 86_400.0

                await self._graph.invoke(self.state)

                if self.state.reproduce:
                    self._reproduce()
                    self.state.reproduce = False

                self._maybe_anchor()

                if not self.state.alive:
                    self._die(self.state.death_reason or "unknown")
                    break

                beats += 1
                self._sleep(self._heartbeat_interval())
        except KillSignal as sig:
            self._halt_on_kill(sig)
        return self.state

    def _heartbeat_interval(self) -> float:
        return self._hp.heartbeat_interval_seconds

    def _anchor_day(self) -> str:
        # One clock for the whole organism: the audit day is the *Darwin's*
        # day (sim or real), never a stray wall-clock fallback. Mixing the two
        # is how the death/kill re-anchor used to look like tampering.
        return time.strftime("%Y-%m-%d", time.gmtime(self._clock()))

    def _maybe_anchor(self) -> None:
        day = self._anchor_day()
        if day != self._anchored_day:
            self.audit.anchor(self._p.anchor_store, day)
            self._anchored_day = day

    def _reproduce(self) -> None:
        profit = self.state.balance_usdc - self.state.profit_baseline_usdc
        result = reproduce(
            parent_identity=self.identity,
            inherited_layer_blobs=self._p.layer_blobs,
            inherited_layer_names=self._p.layer_names,
            generation_recurring_failures=dict(self.state.recurring_failures),
            dead_ends=set(),
            successful_strategy_id=self.strategy.spec.id,
            profit_usdc=profit,
            parent_balance_usdc=self.state.balance_usdc,
            minimum_viable_usdc=self._hp.minimum_viable_balance_usdc,
            profit_transfer_fraction=self._hp.profit_transfer_fraction,
            signer=self._p.signer,
            spawner=self._p.spawner,
            audit=self.audit,
            clock=self._clock,
        )
        # The spawner debited the parent treasury; rebase the profit baseline
        # so the next reproduction measures fresh profit, not the surplus we
        # just handed the child.
        self.state.profit_baseline_usdc = float(self._p.wallet.balance_usdc())
        self.audit.log_event("reproduction.done", result.model_dump())

    # -- shutdown paths ----------------------------------------------------
    def _die(self, reason: str) -> None:
        """Death by bankruptcy (docs/engineering/05). Clean, not a crash."""
        self.audit.log_event("death.begin", {"reason": reason})
        self.audit.anchor(self._p.anchor_store, self._anchor_day())  # final root
        self._p.escalation.post(
            {
                "darwin_id": self.identity.darwin_id,
                "kind": "death",
                "reason": reason,
                "final_balance_usdc": self.state.balance_usdc,
            }
        )
        self.audit.log_event("death.end", {"final_balance_usdc": self.state.balance_usdc})

    def _halt_on_kill(self, sig: KillSignal) -> None:
        """Kill switch flipped (P8). Halt before any further side effect and
        write a deterministic final record."""
        self.state.alive = False
        self.state.halt = True
        self.state.death_reason = f"killed: {sig.reason}"
        self.audit.log_event(
            "kill.halt",
            {"reason": sig.reason, "observed_at": sig.observed_at,
             "balance_usdc": self.state.balance_usdc},
        )
        self.audit.anchor(self._p.anchor_store, self._anchor_day())
        self._p.escalation.post(
            {
                "darwin_id": self.identity.darwin_id,
                "kind": "killed",
                "reason": sig.reason,
            }
        )


def boot(
    platform: Platform,
    *,
    hp: Hyperparameters | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    engine: str = "internal",
) -> DarwinRuntime:
    """Verify the DNA, assert the constitution, wire the machinery, return a
    runnable Darwin. Any trust-root or constitution failure raises here,
    before a single side-effecting line of the organism runs."""
    # 1. The constitution must be internally coherent.
    assert_constitution_invariants()

    # 2. The trust root: every layer signed, ordered, complete, bound to this
    #    lineage. LayerStack.load runs verify_manifest.
    pub = load_platform_public_key(platform.signer.public_key_pem())
    stack = LayerStack.load(platform.manifest, platform.layer_blobs, pub)

    mid = platform.manifest.identity
    identity = Identity(
        darwin_id=mid.darwin_id,
        generation=mid.generation,
        parent_id=mid.parent_id,
        birth_ts=mid.birth_ts,
        lineage=mid.lineage,
        l0_version=mid.l0_version,
    )

    return DarwinRuntime(
        identity=identity,
        platform=platform,
        layer_stack=stack,
        hp=hp or DEFAULT_HYPERPARAMETERS,
        clock=clock,
        sleep=sleep,
        engine=engine,
    )
