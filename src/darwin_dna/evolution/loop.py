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
from typing import Callable, Protocol, runtime_checkable

from ..constitution import assert_constitution_invariants
from ..hyperparameters import DEFAULT_HYPERPARAMETERS, Hyperparameters
from ..identity import Identity
from ..layers import LayerStack
from ..machinery.audit_logger import AuditLogger
from ..machinery.kill_switch import KillSignal, KillSwitch
from ..machinery.pause_switch import PauseSwitch
from ..machinery.ledger import Ledger
from ..machinery.llm_router import LLMRouter
from ..machinery.memory import EpisodicMemory, SemanticMemory
from ..machinery.vault import ScopedVaultView, TieredVault
from ..manifest import Manifest
from ..neurons import NeuronContext, build_catalog
from ..orchestrator_skeleton import OrchestratorContext, OrchestratorState, build_graph
from ..signature import load_platform_public_key
from ..strategies import founding_strategy
from .archive import MutationArchive
from .reproduction import ChildSpawner, PlatformSigner, reproduce


@runtime_checkable
class EscalationInbox(Protocol):
    """The owner notification bus + the supervisor's response channel
    (docs/engineering/08). The genome posts tickets and polls for the owner's
    resolutions; it never blocks waiting — a paused task is parked in working
    memory and resumed when the answer arrives (P4)."""

    def post(self, ticket: dict) -> None: ...

    def poll_responses(self, darwin_id: str) -> list[dict]:
        """Return (and consume) any owner resolutions since the last poll.
        Each: ``{"ticket": id, "decision": "approve"|"deny"|"data",
        "data": {...}}``. Empty list if none."""
        ...


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
        vault_grants=None,
        pause_source=None,
        x402_client=None,
        sandbox=None,
        episodic_memory=None,
        content_classifier=None,
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
        # Where the platform writes one-time owner-approval grants when an
        # escalation is approved (doc 16). Optional: no grant store means
        # sensitive-tier reads simply fail closed.
        self.vault_grants = vault_grants
        # The owner/supervisor pause flag (doc 09). Optional: absent means the
        # Darwin can never be paused, only killed — it still boots and runs.
        self.pause_source = pause_source
        # x402-capable HTTP client (doc 17 §x402). When provided, neurons
        # whose capability_tags overlap {"web.operate", "web.publish"} receive
        # it via NeuronContext.http so HTTP 402 responses are transparently
        # settled against this Darwin's wallet without per-neuron plumbing.
        self.x402_client = x402_client
        # Sandbox substrate (doc 14). When provided, neurons whose
        # capability_tags include ``compute.run`` receive it via
        # NeuronContext.sandbox. The eval_harness also picks it up to run
        # candidate mutations in a clean substrate.
        self.sandbox = sandbox
        # Episodic memory substrate (doc 03). When None the genome uses its
        # deterministic default — ``EpisodicMemory`` with exact-signature +
        # token-overlap recall, on which the 90-day sim and cross-engine
        # parity depend. The platform MAY inject a per-Darwin vector store
        # (e.g. ``LanceDbEpisodicMemory``) with the same surface; embeddings
        # cannot live in the DNA itself (model lock-in P6, heavy dep P9), so
        # this is the seam they enter through.
        self.episodic_memory = episodic_memory
        # Outbound content-safety classifier (doc 10). When provided, the
        # loop injects it into publishing neurons' NeuronContext so the
        # ContentSafetyGate runs a real classifier instead of only its
        # deterministic substring backstop. Optional — None keeps the backstop.
        self.content_classifier = content_classifier


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
        self.pause = PauseSwitch(getattr(platform, "pause_source", None))
        self.ledger = Ledger(identity.darwin_id)
        self.archive = MutationArchive(hp.archive_max_entries)
        # Episodic + semantic memory (docs/engineering/03). Rebuilt fresh on a
        # sandbox-replacement resume, like the ledger and the archive: the
        # durable, replayable record of a life is the audit chain, not these
        # in-life caches. Semantic is the staging ground for the next layer.
        #
        # The deterministic EpisodicMemory is the genome default (the sim and
        # cross-engine parity depend on it). The platform MAY inject a
        # production substrate via ``platform.episodic_memory`` — a per-Darwin
        # LanceDB vector store with the same surface — without changing this
        # boot path. Embeddings cannot live in the DNA itself (P6, P9), so
        # they enter through this seam.
        # `is None`, NOT `or` — an empty injected substrate (e.g. a fresh
        # LanceDbEpisodicMemory whose __len__ is 0) is truthy-falsy in
        # Python and `or` would silently fall back to the default.
        _injected_episodic = getattr(platform, "episodic_memory", None)
        self.episodic = (
            _injected_episodic
            if _injected_episodic is not None
            else EpisodicMemory(hp.episodic_max_entries)
        )
        self.semantic = SemanticMemory(hp.semantic_max_staged)
        # The tiered vault wraps the raw per-Darwin namespace and enforces the
        # public/scoped/sensitive read model (doc 16). Each neuron is handed a
        # view bound to its own capability tags, never this object.
        self._vault = TieredVault(
            platform.vault,
            darwin_id=identity.darwin_id,
            grants=platform.vault_grants,
        )
        self.llm = LLMRouter(platform.llm_transport, hp, clock=clock)
        self.catalog = build_catalog(
            wallet=platform.wallet, provision=platform.provision
        )
        # Path-C admission gate (doc 04 phase 3). The orchestrator's
        # ``propose_mutation`` can emit a NeuronProposal; the
        # ``apply_mutation`` node calls ``gate.submit + gate.admit``
        # after the eval harness scored it favourably; admitted
        # neurons are merged into the catalog so the next dispatch
        # can route to them. Bounded by ``NeuronAdmissionGate.max_admitted``
        # so a Darwin can't flood its catalog.
        # OFF by default (doc 04: "we will not enable Path C in the initial
        # launch"). When disabled, the gate is None and apply_mutation never
        # admits a Darwin-authored neuron at all. When enabled, admitted
        # proposals still pass through a staging quarantine + operator review
        # (see ``path_c_require_human_review``) before joining the live catalog.
        if getattr(hp, "path_c_enabled", False):
            from ..machinery.neuron_proposal import NeuronAdmissionGate
            self.neuron_admission = NeuronAdmissionGate()
        else:
            self.neuron_admission = None
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

        # Platform-staged amendment for the next reproduction (doc 02 §amendment,
        # doc 09 platform-operator path). One-shot — consumed by ``_reproduce``
        # and cleared after. Not in OrchestratorState because the platform sets
        # this from outside the genome via :meth:`install_amendment`; the
        # genome's perceive/plan/dispatch cycle never reads or writes it.
        self._pending_amendment: tuple[bytes, str] | None = None

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
            ledger=self.ledger,
            archive=self.archive,
            episodic=self.episodic,
            semantic=self.semantic,
            build_neuron_context=self._build_neuron_context,
            clock=clock,
        )
        # Path-C plumbing on the context so the orchestrator nodes can
        # submit + admit neuron proposals without reaching past the
        # context bundle into the runtime (doc 04 phase 3).
        ctx.neuron_admission = self.neuron_admission
        ctx.admit_neuron_proposal_helper = self.admit_neuron_proposal
        ctx.path_c_require_human_review = getattr(
            self._hp, "path_c_require_human_review", True
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
        # Hand the neuron a vault view scoped to its *own* capability tags. It
        # cannot read another capability's scoped secrets because it never
        # holds the others (doc 16, isolation). Its default write/read scope is
        # one of its own tags so a credential it persists round-trips.
        neuron = self.catalog.get(neuron_name)
        tags = frozenset(getattr(neuron, "capability_tags", frozenset()))
        scoped_vault = ScopedVaultView(
            self._vault,
            capability_tags=tags,
            default_scope=(sorted(tags)[0] if tags else "shared"),
        )
        nctx = NeuronContext(
            darwin_id=self.identity.darwin_id,
            llm=self.llm,
            audit=self.audit,
            vault=scoped_vault,
            lessons=relevant,
            now=self._clock,
        )
        # The browser substrate is a capability, granted only to the neuron
        # that needs it.
        if neuron_name == "browser.operator" and self._p.browser is not None:
            nctx.browser = self._p.browser
        # x402 HTTP client (doc 17): grant only to neurons that make outbound
        # web requests. Guards against accidentally giving treasury-settling
        # HTTP capability to a neuron that has no business making web calls.
        if (
            getattr(self._p, "x402_client", None) is not None
            and tags & {"web.operate", "web.publish", "web.signup"}
        ):
            nctx.http = self._p.x402_client
        # Sandbox (doc 14): granted to neurons that need to run code in
        # isolation. Same gate as the http one — capability tag is the only
        # path to the capability.
        if (
            getattr(self._p, "sandbox", None) is not None
            and "compute.run" in tags
        ):
            nctx.sandbox = self._p.sandbox
        # Content safety classifier (doc 10): granted to publishing neurons.
        # The ContentSafetyGate still runs its deterministic backstop when
        # this is None, so screening is never skipped on a publish path.
        if (
            getattr(self._p, "content_classifier", None) is not None
            and tags & {"content.publish", "web.publish"}
        ):
            nctx.content_classifier = self._p.content_classifier
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

                # Kill is sacred (doc 09: honored within 5s) and must be
                # polled at the loop boundary too — a paused Darwin must not
                # be a Darwin shielded from the kill switch. Polling here
                # before the pause check ensures a kill flipped *during* a
                # pause halts on the next drive instead of indefinitely
                # parking. The internal runner polls kill again at every
                # node boundary; this is the outer-loop heartbeat boundary.
                self.kill.check()

                # Owner/supervisor pause (doc 09 level-3). The loop boundary
                # is a safe checkpoint — the previous heartbeat checkpointed
                # at every node. Halt here and return WITHOUT dying: the same
                # life resumes from this checkpoint when ``run`` is next
                # driven and the flag has cleared. Distinct from kill, which
                # is terminal and unwinds via KillSignal.
                if self.pause.paused():
                    if not self.state.paused:
                        self.state.paused = True
                        self.audit.log_event(
                            "owner_action",
                            {"kind": "pause",
                             "reason": self.pause.reason() or "owner paused",
                             "tick": self.state.tick},
                        )
                        self._maybe_anchor()  # seal the chain at the pause
                    break
                if self.state.paused:  # the flag was lifted — resume the life
                    self.state.paused = False
                    self.audit.log_event(
                        "owner_action",
                        {"kind": "resume", "tick": self.state.tick},
                    )

                self.state.days_elapsed = (
                    self._clock() - self.identity.birth_ts
                ) / 86_400.0

                # Always rebind: the internal runner mutates state in place,
                # but the LangGraph runner returns a fresh state object — the
                # loop must not depend on which engine is wired.
                self.state = await self._graph.invoke(self.state)

                if self.state.reproduce:
                    self._reproduce()
                    self.state.reproduce = False
                    # Consume the platform-set force flag too so a second
                    # heartbeat doesn't re-trigger off the same request.
                    self.state.force_reproduce = False

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

    def admit_neuron_proposal(self, name: str) -> None:
        """Admit a pending Path-C proposal into the live catalog
        (docs/engineering/04 phase 3). Called by ``apply_mutation`` after
        the eval harness scored the proposal as a net win. Refuses on:

        - unknown proposal name (the gate raises NeuronProposalError)
        - admitted-count cap reached (likewise)
        - the resulting InterpretedNeuron fails the loader's contract
          check (we surface that as a refusal + leave the catalog as is)

        On success, the catalog gains the new neuron AND the audit
        chain records the admission so an operator can replay which
        Darwin learned what."""
        from ..machinery.neuron_loader import load_catalog
        from ..machinery.neuron_proposal import NeuronProposalError

        if self.neuron_admission is None:
            # Path-C is disabled (doc 04 default): there is no gate, so an
            # operator admit request is refused cleanly rather than raising
            # an AttributeError on None.
            self.audit.log_event(
                "neuron_proposal.admission_refused",
                {"name": name, "reason": "path-c disabled"},
            )
            raise NeuronProposalError(
                "Path-C is disabled; enable path_c_enabled to admit neurons"
            )
        try:
            neuron = self.neuron_admission.admit(name)
        except NeuronProposalError as exc:
            self.audit.log_event(
                "neuron_proposal.admission_refused",
                {"name": name, "reason": str(exc)},
            )
            raise
        # Re-run the catalog loader's contract check on the combined set
        # so an admitted Path-C neuron can't accidentally collide with
        # the founding catalog or violate the one-money-door rule.
        try:
            new_catalog = load_catalog(
                list(self.catalog.values()) + [neuron],
            )
        except Exception as exc:
            # Revoke + audit: contract check is the trust gate.
            self.neuron_admission.revoke(name)
            self.audit.log_event(
                "neuron_proposal.contract_check_failed",
                {"name": name, "reason": str(exc)},
            )
            raise
        self.catalog = new_catalog
        self.audit.log_event(
            "neuron_proposal.admitted",
            {"name": name, "tags": sorted(neuron.capability_tags)},
        )

    def install_amendment(self, blob: bytes, name: str) -> None:
        """Stage a platform-supplied amendment layer for the NEXT reproduction
        (docs/engineering/02 §amendment). One-shot: consumed by the upcoming
        ``_reproduce``, then cleared. Called by the platform supervisor; the
        genome's own cycle never invokes this."""
        if not isinstance(blob, (bytes, bytearray)):
            raise TypeError("amendment blob must be bytes")
        if not name or not isinstance(name, str):
            raise ValueError("amendment name must be a non-empty string")
        self._pending_amendment = (bytes(blob), name)

    def has_pending_amendment(self) -> bool:
        return self._pending_amendment is not None

    def _reproduce(self) -> None:
        profit = self.state.balance_usdc - self.state.profit_baseline_usdc
        # Drain the staging ground: the strongest-evidence runtime lessons get
        # a chance at a sealed DNA layer the child inherits forever (doc 03).
        # drain() also clears it — semantic memory is windowed per generation,
        # exactly like the profit baseline and the counter-pressures below.
        staged_semantic = [s.model_dump() for s in self.semantic.drain()]
        amendment = self._pending_amendment
        # Clear BEFORE the call so an exception mid-reproduction doesn't leave
        # the amendment staged for a second attempt.
        self._pending_amendment = None
        result = reproduce(
            parent_identity=self.identity,
            inherited_layer_blobs=self._p.layer_blobs,
            inherited_layer_names=self._p.layer_names,
            generation_recurring_failures=dict(self.state.recurring_failures),
            # Inherit the parent's accumulated dead-ends (negative lessons) so
            # the child doesn't re-explore mutation classes the lineage already
            # proved fruitless (doc 06). Was hardcoded to set() — the
            # cross-generation negative-lesson path.
            dead_ends=set(self.archive.dead_ends()),
            successful_strategy_id=self.strategy.spec.id,
            profit_usdc=profit,
            staged_semantic=staged_semantic,
            parent_balance_usdc=self.state.balance_usdc,
            minimum_viable_usdc=self._hp.minimum_viable_balance_usdc,
            profit_transfer_fraction=self._hp.profit_transfer_fraction,
            generation_soft_cap=self._hp.generation_soft_cap,
            signer=self._p.signer,
            spawner=self._p.spawner,
            audit=self.audit,
            clock=self._clock,
            amendment=amendment,
        )
        # The spawner debited the parent treasury; rebase the profit baseline
        # so the next reproduction measures fresh profit, not the surplus we
        # just handed the child. The fitness counter-pressures are windowed
        # the same way — reset them here so each generation is judged on its
        # own escalation quality and policy record, not its ancestors'.
        self.state.profit_baseline_usdc = float(self._p.wallet.balance_usdc())
        self.state.escalations_approved = 0
        self.state.escalations_rejected = 0
        self.state.policy_violations = 0
        self.audit.log_event("reproduction.done", result.model_dump())

    # -- shutdown paths ----------------------------------------------------
    def _settle_commitments(self) -> dict:
        """Death-sequence step 3 (docs/engineering/05): settle or abandon
        in-flight commitments. A Layer-0 Darwin has no auto-settle path for
        a parked unit of work — resuming one needs the owner — so it
        *abandons* them cleanly and records each abandonment. Nothing is
        silently lost: the owner and the platform escalation router can see
        every parked ticket was closed by death, not dropped on the floor.
        Cost-weighted 'settle vs abandon' is a soma refinement."""
        open_tickets = list(self.state.open_escalations.keys())
        for ticket in open_tickets:
            esc = self.state.open_escalations.get(ticket, {})
            self.audit.log_event(
                "death.commitment_abandoned",
                {
                    "ticket": ticket,
                    "neuron": esc.get("neuron"),
                    "blocker_kind": esc.get("blocker_kind"),
                    "disposition": "abandoned",
                },
            )
        summary = {
            "abandoned_escalations": len(open_tickets),
            "abandoned_tickets": open_tickets,
            "dropped_capability_requests": len(
                self.state.pending_capability_requests
            ),
            "dropped_resume_queue": len(self.state.resume_queue),
        }
        self.state.open_escalations.clear()
        self.state.pending_escalations.clear()
        self.state.resume_queue.clear()
        self.state.pending_capability_requests.clear()
        self.audit.log_event("death.commitments_settled", summary)
        return summary

    def _die(self, reason: str) -> None:
        """Death by bankruptcy (docs/engineering/05). Clean, not a crash."""
        self.audit.log_event("death.begin", {"reason": reason})
        # Step 3: settle/abandon in-flight commitments before the final root
        # so the abandonments are inside the anchored chain.
        settled = self._settle_commitments()
        self.audit.anchor(self._p.anchor_store, self._anchor_day())  # final root
        self._p.escalation.post(
            {
                "darwin_id": self.identity.darwin_id,
                "kind": "death",
                "reason": reason,
                "final_balance_usdc": self.state.balance_usdc,
                "abandoned_commitments": settled["abandoned_escalations"],
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
