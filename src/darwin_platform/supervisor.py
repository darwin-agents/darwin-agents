"""The Darwin Supervisor (docs/engineering/08).

The Supervisor is the single writer for Darwin lifecycle transitions. In doc-08
production it is a K8s-aware service spawning microVMs; in this first pass it
hosts each Darwin as an asyncio task inside its own process, with a fake stack
(``darwin_fakes.make_platform``) standing in for the platform's external ports.
The Supervisor's public surface is the doc-08 endpoints; HTTP wiring lives in
``api.py``, so the class itself can be driven in-process by tests.

Design notes:

- One asyncio task per Darwin, calling ``runtime.run(max_heartbeats=1)`` in a
  loop. We set ``sleep=lambda _: None`` on the runtime so the in-loop pacing
  is a no-op — the Supervisor controls the gap between heartbeats with an
  ``asyncio.sleep`` so the event loop never blocks even with many Darwins.
- Kill / pause / resume are platform-side flag flips on the fake stack. The
  genome polls those flags at every node boundary (P8) and at the heartbeat
  boundary (doc 09 pause); we never reach into the runtime's state to halt it.
- Force-reproduce sets ``state.reproduce = True`` *between* heartbeats — the
  genome's reproduction guards (treasury minimum, fitness window) still run on
  the next ``check_reproduction`` and may refuse. Forcing isn't a bypass.
- Lineage is tracked by observing the fake spawner: when ``stack.spawner``
  grows, the Supervisor auto-boots the child with ``make_child_platform`` so
  the same Supervisor instance drives the next generation. This is what a real
  platform does (the doc-08 ``POST /platform/signing/dna`` + spawn pair, in
  one process).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from darwin_dna.evolution import DarwinRuntime, boot
from darwin_fakes import FakeStack, make_child_platform
from darwin_fakes.fake_platform import SigningService

from .escalation import EscalationRouter, NotificationSink, RoutedTicket
from .monitoring import (
    Alert,
    AlertSink,
    DarwinSignals,
    Monitor,
    Quotas,
    Severity,
)
from .tracing import annotate_span_with_runtime, span


# -- errors ----------------------------------------------------------------
class SupervisorError(Exception):
    """Base for supervisor-side rejections (unknown id, bad state)."""


class UnknownDarwin(SupervisorError):
    def __init__(self, darwin_id: str) -> None:
        super().__init__(f"unknown darwin: {darwin_id}")
        self.darwin_id = darwin_id


# -- public-shape models ---------------------------------------------------
@dataclass(frozen=True)
class DarwinStatus:
    """The doc-08 status surface for one Darwin. Frozen so the API layer can
    serialize it without worrying about mutation between read and send."""

    darwin_id: str
    generation: int
    parent_id: str | None
    l0_version: str
    birth_ts: float
    spawned_at: float
    alive: bool
    paused: bool
    killed: bool
    quarantined: bool
    quarantine_reason: str | None
    death_reason: str | None
    balance_usdc: float
    tick: int
    days_elapsed: float
    open_escalations: int


@dataclass(frozen=True)
class QuarantineState:
    """Doc-09 level-4 graded response. Distinct from owner pause: triggered
    by a confirmed policy/compliance issue, not by the owner; cannot be
    cleared by the owner's ``resume`` endpoint; requires an explicit platform
    ``unquarantine`` (or a kill)."""

    active: bool
    reason: str | None = None
    operator: str | None = None
    applied_ts: float | None = None


@dataclass(frozen=True)
class LineageEdge:
    parent_id: str
    child_id: str
    spawned_at: float
    budget_usdc: float


# -- per-Darwin handle -----------------------------------------------------
@dataclass
class _Handle:
    """Internal: every running Darwin's bookkeeping. Not exposed."""

    darwin_id: str
    stack: FakeStack
    runtime: DarwinRuntime
    spawned_at: float
    parent_id: str | None = None
    children: list[str] = field(default_factory=list)
    children_observed: int = 0
    reproduce_requested: bool = False
    killed: bool = False
    task: asyncio.Task | None = None
    # Tickets we have already routed to the genome's inbox so we don't
    # double-respond. Indexed by ticket id.
    handled_responses: set[str] = field(default_factory=set)
    # Cursor into stack.escalation.tickets; new posts past this index get
    # registered with the platform router on the next heartbeat boundary.
    escalation_observed: int = 0
    # Cycle counter so the monitor doesn't run every single tick — most
    # signals (spend velocity over an hour, failure rate, escalation rate)
    # do not need sub-second resolution.
    drive_cycles: int = 0
    auto_paused_for: str | None = None  # alert kind, if we auto-paused
    auto_throttled: set[str] = field(default_factory=set)  # alert kinds we
    # already auto-throttled — idempotent per (darwin, kind), cleared on
    # an explicit clear_throttle or resume.
    quarantine: QuarantineState = field(
        default_factory=lambda: QuarantineState(active=False)
    )
    # Set when a forced reproduction with amendment is in flight. After the
    # next child of this Darwin is observed, the supervisor kills the
    # parent — doc 02's "the parent dies; the child carries the amendment".
    kill_after_reproduction_reason: str | None = None


# -- the supervisor --------------------------------------------------------
class Supervisor:
    """Lifecycle owner for a fleet of in-process Darwins.

    Construct one per process. Methods are coroutines because each operation
    may touch the runtime that the per-Darwin task also touches; running on
    the same event loop keeps everything serialized without explicit locks.
    """

    def __init__(
        self,
        *,
        heartbeat_seconds: float = 0.001,
        clock: Callable[[], float] = time.time,
        notification_sink: NotificationSink | None = None,
        alert_sink: AlertSink | None = None,
        quotas: Quotas | None = None,
        auto_pause_on_critical: bool = False,
        auto_throttle_on_warn: bool = False,
        monitor_every_n_ticks: int = 5,
    ) -> None:
        # Loop pacing: between heartbeats the supervisor's drive task awaits
        # for this long. Default tiny — tests run as fast as the event loop;
        # the API runner can override.
        self._heartbeat = float(heartbeat_seconds)
        self._clock = clock
        self._darwins: dict[str, _Handle] = {}
        self._lineage: list[LineageEdge] = []
        self._closed = False
        # The platform-side escalation router (doc 08). Tickets the genome
        # posts to its inbox get observed at the heartbeat boundary, routed
        # through this object, and notified via the (optional) sink.
        self._router = EscalationRouter(sink=notification_sink, clock=clock)
        # The platform-side anomaly monitor (doc 08/09). Polls each Darwin
        # every N drive cycles, runs the detectors, dispatches alerts via the
        # (optional) sink. ``auto_pause_on_critical`` is opt-in: a tight
        # quota could otherwise auto-pause the fleet on a misconfiguration.
        self._monitor = Monitor(
            quotas=quotas, sink=alert_sink, clock=clock
        )
        self._auto_pause_on_critical = bool(auto_pause_on_critical)
        self._monitor_every_n = max(1, int(monitor_every_n_ticks))
        # Doc 09 level 2: rate-limit the offending resource on a sustained
        # WARN. Off by default so a misconfigured detector cannot throttle
        # the fleet. When on, a WARN spend_velocity halves the reasoning
        # tier cap; a WARN failure_rate halves the coding tier cap.
        self._auto_throttle_on_warn = bool(auto_throttle_on_warn)
        # Single platform-side signing identity (doc 02, doc 18 §"Signing and
        # release"). One HSM key in reality; one SigningService here, shared
        # across every Darwin spawned by this Supervisor instance so all
        # manifests verify against the same public key. The signing private
        # key never leaves this object — the genome only ever sees PEM.
        self._signer = SigningService()

    # -- spawn (doc 08: POST /darwins) ------------------------------------
    async def spawn(
        self,
        *,
        opening_usdc: float = 100.0,
        escalation_auto: str | None = "approve",
        parent_id: str | None = None,
    ) -> DarwinStatus:
        """Mint genesis, boot, start the drive task. Returns the new Darwin's
        status. ``parent_id`` is metadata only for genesis spawns — children
        born from a reproduction event are picked up automatically via the
        spawner observer."""
        if self._closed:
            raise SupervisorError("supervisor is closed")
        with span(
            "supervisor.spawn",
            attributes={"opening_usdc": opening_usdc, "parent_id": parent_id},
        ) as s:
            stack = FakeStack(
                opening_usdc=opening_usdc,
                clock=self._clock,
                escalation_auto=escalation_auto,
                signer=self._signer,
            )
            # Genome's _sleep set to no-op: the supervisor schedules the gaps
            # between heartbeats itself with asyncio.sleep, so the event loop
            # never blocks on time.sleep even with many Darwins on one loop.
            runtime = boot(stack.platform(), sleep=lambda _: None)
            handle = _Handle(
                darwin_id=runtime.identity.darwin_id,
                stack=stack,
                runtime=runtime,
                spawned_at=self._clock(),
                parent_id=parent_id,
            )
            self._darwins[handle.darwin_id] = handle
            annotate_span_with_runtime(s, runtime)
            handle.task = asyncio.create_task(
                self._drive(handle), name=f"darwin:{handle.darwin_id}"
            )
            return self._status(handle)

    # -- status (doc 08: GET /darwins/{id}) -------------------------------
    async def status(self, darwin_id: str) -> DarwinStatus:
        return self._status(self._get(darwin_id))

    # -- pause / resume (doc 08: POST /darwins/{id}/pause|resume) ---------
    async def pause(self, darwin_id: str, *, reason: str = "owner paused") -> None:
        """Flip the platform pause flag. The genome notices at the next
        heartbeat boundary, anchors, and breaks — same life, not a death."""
        with span("supervisor.pause", darwin_id=darwin_id,
                  attributes={"reason": reason}) as s:
            h = self._get(darwin_id)
            if h.killed:
                raise SupervisorError(
                    f"darwin {darwin_id} is killed; pause is a no-op"
                )
            annotate_span_with_runtime(s, h.runtime)
            h.stack.pause.pause(reason)

    async def resume(self, darwin_id: str) -> None:
        with span("supervisor.resume", darwin_id=darwin_id) as s:
            h = self._get(darwin_id)
            if h.killed:
                raise SupervisorError(f"darwin {darwin_id} is killed; cannot resume")
            if h.quarantine.active:
                raise SupervisorError(
                    f"darwin {darwin_id} is quarantined "
                    f"(reason: {h.quarantine.reason!r}); requires unquarantine, "
                    f"not resume"
                )
            annotate_span_with_runtime(s, h.runtime)
            h.stack.pause.resume()
            # Clear the auto-pause latch so a subsequent CRITICAL can pause
            # again if the breach is still live. (A repeat breach is a
            # different alert event from the operator's perspective.)
            h.auto_paused_for = None

    # -- platform-side quarantine (doc 08/09 level 4) ---------------------
    async def quarantine(
        self,
        darwin_id: str,
        *,
        reason: str,
        operator: str = "platform",
    ) -> None:
        """Move a Darwin to doc-09 level 4: pause + flag for platform review.
        Cannot be cleared via owner ``resume`` — only ``unquarantine`` or a
        kill. Idempotent: re-quarantining records the new reason/operator
        but does not double-pause."""
        with span(
            "supervisor.quarantine",
            darwin_id=darwin_id,
            attributes={"reason": reason, "operator": operator},
        ) as s:
            h = self._get(darwin_id)
            if h.killed:
                raise SupervisorError(
                    f"darwin {darwin_id} is killed; quarantine is a no-op"
                )
            annotate_span_with_runtime(s, h.runtime)
            h.quarantine = QuarantineState(
                active=True,
                reason=reason,
                operator=operator,
                applied_ts=self._clock(),
            )
            # Pause if not already paused; the pause flag is shared with owner
            # pause so a Darwin already paused stays paused under quarantine.
            try:
                h.stack.pause.pause(f"quarantine:{reason}")
            except Exception:
                # The fake should not raise here, but a misbehaving pause
                # source must not block the quarantine state.
                pass

    async def unquarantine(self, darwin_id: str, *, operator: str = "platform") -> None:
        """Clear the quarantine flag. Does NOT auto-resume — an operator
        clearing quarantine has to make a separate ``resume`` call so the
        two state transitions are independently auditable."""
        with span(
            "supervisor.unquarantine",
            darwin_id=darwin_id,
            attributes={"operator": operator},
        ) as s:
            h = self._get(darwin_id)
            if not h.quarantine.active:
                return
            annotate_span_with_runtime(s, h.runtime)
            h.quarantine = QuarantineState(active=False)

    # -- platform-side throttle (doc 08, doc 09 level 2) ------------------
    async def throttle_llm_tier(
        self,
        darwin_id: str,
        *,
        tier: str,
        max_daily_calls: int,
    ) -> dict[str, Any]:
        """Install a tighter per-day cap on one LLM tier. The genome's
        :class:`LLMRouter` takes ``min(hp cap, throttle)``, so a throttle
        can only TIGHTEN; the constitution-level cap remains the ceiling."""
        from darwin_dna.machinery.llm_router import Tier

        try:
            t = Tier(tier)
        except ValueError as exc:
            raise SupervisorError(f"unknown tier: {tier!r}") from exc
        with span(
            "supervisor.throttle_llm_tier",
            darwin_id=darwin_id,
            attributes={"tier": tier, "max_daily_calls": max_daily_calls},
        ) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            h.runtime.llm.set_throttle(t, max_daily_calls)
        return {"tier": tier, "max_daily_calls": int(max_daily_calls),
                "throttles": h.runtime.llm.throttles()}

    async def clear_llm_throttle(
        self,
        darwin_id: str,
        *,
        tier: str | None = None,
    ) -> dict[str, Any]:
        """Clear the platform throttle for ``tier`` (or all tiers when
        ``tier is None``)."""
        from darwin_dna.machinery.llm_router import Tier

        t: Tier | None = None
        if tier is not None:
            try:
                t = Tier(tier)
            except ValueError as exc:
                raise SupervisorError(f"unknown tier: {tier!r}") from exc
        with span(
            "supervisor.clear_llm_throttle",
            darwin_id=darwin_id,
            attributes={"tier": tier},
        ) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            h.runtime.llm.clear_throttle(t)
            # An explicit clear also clears the auto-throttle latches so a
            # follow-up WARN can throttle again.
            if tier is None:
                h.auto_throttled.clear()
        return {"throttles": h.runtime.llm.throttles()}

    async def get_llm_throttles(self, darwin_id: str) -> dict[str, int]:
        h = self._get(darwin_id)
        return h.runtime.llm.throttles()

    # -- platform-side wallet policy (doc 08 POST /platform/wallets/...) --
    async def set_wallet_policy(
        self,
        darwin_id: str,
        *,
        per_action_cap_usdc: float | None = None,
        per_day_cap_usdc: float | None = None,
        per_lifetime_cap_usdc: float | None = None,
    ) -> dict[str, Any]:
        """Tighten a Darwin's per-action / per-day / per-lifetime spend caps
        below the genome's hard caps. The platform may only LOWER caps (the
        constitution's hard caps are inviolable); the wallet enforces that.
        The same caps flow into the genome's policy engine via the wallet's
        ``owner_soft_caps`` method, so a refused spend is logged with policy
        rationale, not as a generic wallet_error. Returns the new effective
        policy."""
        with span(
            "supervisor.set_wallet_policy",
            darwin_id=darwin_id,
            attributes={
                "per_action_cap_usdc": per_action_cap_usdc,
                "per_day_cap_usdc": per_day_cap_usdc,
                "per_lifetime_cap_usdc": per_lifetime_cap_usdc,
            },
        ) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            wallet = h.stack.wallet
            if not hasattr(wallet, "set_policy"):
                raise SupervisorError(
                    "wallet does not support platform-installed policy"
                )
            return wallet.set_policy(
                per_action_cap_usdc=per_action_cap_usdc,
                per_day_cap_usdc=per_day_cap_usdc,
                per_lifetime_cap_usdc=per_lifetime_cap_usdc,
            )

    async def get_wallet_policy(self, darwin_id: str) -> dict[str, Any]:
        h = self._get(darwin_id)
        wallet = h.stack.wallet
        if not hasattr(wallet, "policy"):
            raise SupervisorError("wallet does not expose policy")
        return wallet.policy()

    # -- platform signing service surface (doc 08) ------------------------
    def signing_public_key_pem(self) -> bytes:
        """The platform's signing public key in PEM. Operators verify against
        this to confirm any DNA manifest signature in the system. In a real
        deployment this comes from an HSM; here it's the fake SigningService
        the Supervisor instantiated and shares across every Darwin it spawns.
        Available regardless of whether any Darwins have been spawned yet."""
        return self._signer.public_key_pem()

    # -- kill (doc 08: POST /darwins/{id}/kill) ---------------------------
    async def kill(self, darwin_id: str, *, reason: str = "owner kill") -> None:
        """Flip the platform kill flag. The genome polls it at the loop
        boundary and at every node boundary (doc 09: ≤5s latency), then runs
        ``_halt_on_kill`` which writes the final audit record and anchors."""
        with span("supervisor.kill", darwin_id=darwin_id,
                  attributes={"reason": reason}) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            h.killed = True
            h.stack.kill.flip(reason)

    # -- force reproduction (doc 08: POST /darwins/{id}/reproduce/force) --
    async def force_reproduce(self, darwin_id: str) -> None:
        """Set the genome's reproduce flag for the next heartbeat. The
        genome still runs its own treasury/fitness gates inside ``_reproduce``;
        forcing requests, it does not bypass."""
        with span("supervisor.force_reproduce", darwin_id=darwin_id) as s:
            h = self._get(darwin_id)
            if not h.runtime.state.alive:
                raise SupervisorError(f"darwin {darwin_id} is not alive")
            annotate_span_with_runtime(s, h.runtime)
            h.reproduce_requested = True

    # -- forced reproduction with amendment (doc 02 + doc 09) -------------
    async def force_reproduce_with_amendment(
        self,
        darwin_id: str,
        *,
        amendment_blob: bytes,
        amendment_name: str,
        kill_parent_reason: str = "amendment reproduction",
    ) -> None:
        """The platform-supervisor pathway for shipping a constitutional
        update (doc 02 §amendment). Stages the amendment on the runtime,
        requests reproduction at the next heartbeat, and marks the parent
        for kill after the child is observed. Doc 02: "the child inherits
        the amendment; the parent dies"."""
        with span(
            "supervisor.force_reproduce_with_amendment",
            darwin_id=darwin_id,
            attributes={"amendment_name": amendment_name},
        ) as s:
            h = self._get(darwin_id)
            if not h.runtime.state.alive:
                raise SupervisorError(f"darwin {darwin_id} is not alive")
            if h.killed:
                raise SupervisorError(
                    f"darwin {darwin_id} is killed; cannot reproduce"
                )
            annotate_span_with_runtime(s, h.runtime)
            # The trust-root layer-count cap (doc 02 / manifest.py) excludes
            # platform amendments by their name prefix. Refuse mis-named
            # amendments at the API edge with a clear error rather than
            # letting them blow up in ``reproduce.verify_manifest``.
            from darwin_dna.manifest import AMENDMENT_LAYER_PREFIX

            if not amendment_name.startswith(AMENDMENT_LAYER_PREFIX):
                raise SupervisorError(
                    f"amendment_name must start with "
                    f"{AMENDMENT_LAYER_PREFIX!r}, got {amendment_name!r}"
                )
            try:
                h.runtime.install_amendment(amendment_blob, amendment_name)
            except (TypeError, ValueError) as exc:
                raise SupervisorError(str(exc)) from exc
            h.reproduce_requested = True
            h.kill_after_reproduction_reason = kill_parent_reason

    # -- escalations (doc 08: POST .../escalations/{eid}/respond) ---------
    async def respond_escalation(
        self,
        darwin_id: str,
        *,
        escalation_id: str,
        decision: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Route an owner decision to the genome's escalation inbox AND
        record it on the platform-side router. Two writes — the inbox is
        what the genome polls in ``perceive``; the router is what the
        dashboard reads. They must agree."""
        if decision not in ("approve", "deny", "data"):
            raise SupervisorError(f"unknown decision: {decision}")
        with span(
            "supervisor.respond_escalation",
            darwin_id=darwin_id,
            attributes={"escalation_id": escalation_id, "decision": decision},
        ) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            h.stack.escalation.respond(escalation_id, decision, data or {})
            self._router.mark_responded(escalation_id, decision, data or {})
            h.handled_responses.add(escalation_id)

    # -- lineage (doc 08: GET /darwins/{id}/lineage) ----------------------
    async def lineage(self, darwin_id: str) -> dict[str, Any]:
        """Return the ancestry + descendants the supervisor has seen for this
        Darwin. The tree spans Darwins booted by *this* supervisor — a Darwin
        whose parent was hosted by a different supervisor only knows its
        own ``parent_id`` metadata."""
        h = self._get(darwin_id)
        return {
            "darwin_id": darwin_id,
            "parent_id": h.parent_id,
            "children": list(h.children),
            "ancestry": self._ancestry(darwin_id),
            "edges": [
                {
                    "parent_id": e.parent_id,
                    "child_id": e.child_id,
                    "spawned_at": e.spawned_at,
                    "budget_usdc": e.budget_usdc,
                }
                for e in self._lineage
                if e.parent_id == darwin_id or e.child_id == darwin_id
            ],
        }

    # -- ledger (doc 08: GET /darwins/{id}/ledger) ------------------------
    async def ledger(
        self, darwin_id: str, *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """Return movements from the per-Darwin wallet. In production the
        ledger is reconciled against the on-chain wallet; here we read the
        wallet's authoritative movement feed directly (same shape)."""
        h = self._get(darwin_id)
        movements = list(h.stack.wallet.movements())
        total = len(movements)
        page = movements[offset : offset + limit]
        return {
            "darwin_id": darwin_id,
            "total": total,
            "offset": offset,
            "limit": limit,
            "balance_usdc": h.stack.wallet.balance_usdc(),
            "movements": page,
        }

    # -- events (doc 08: GET /darwins/{id}/events) ------------------------
    async def events(
        self, darwin_id: str, *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """Return the hash-chained audit log entries the genome wrote. The
        chain is over the whole sink; the supervisor filters to this Darwin's
        id so multi-tenant inspection still works."""
        h = self._get(darwin_id)
        all_records = h.stack.audit_sink.read_all()
        own = [r for r in all_records if r.get("darwin_id") == darwin_id]
        total = len(own)
        page = own[offset : offset + limit]
        return {
            "darwin_id": darwin_id,
            "total": total,
            "offset": offset,
            "limit": limit,
            "events": page,
        }

    # -- escalation inbox view (not a doc-08 endpoint; useful to UIs) -----
    async def open_escalations(
        self, darwin_id: str, *, include_resolved: bool = False
    ) -> list[dict[str, Any]]:
        """Open (and optionally resolved) tickets the platform-side router
        has classified for this Darwin. Each row carries the ticket class,
        deadline, age, remaining seconds, and any owner response — exactly
        the shape an owner dashboard needs to render countdowns."""
        self._get(darwin_id)  # validate id (raises UnknownDarwin)
        now = self._clock()
        return [
            tk.to_dict(now)
            for tk in self._router.for_darwin(
                darwin_id, include_resolved=include_resolved
            )
        ]

    async def get_escalation(
        self, darwin_id: str, escalation_id: str
    ) -> dict[str, Any]:
        """One-ticket detail. 404-ish (UnknownDarwin / KeyError) on miss."""
        self._get(darwin_id)
        tk = self._router.get(escalation_id)
        if tk is None or tk.darwin_id != darwin_id:
            raise SupervisorError(f"unknown escalation: {escalation_id}")
        return tk.to_dict(self._clock())

    # -- supervisor shutdown ----------------------------------------------
    async def shutdown(self, *, kill_running: bool = True) -> None:
        """Stop driving every Darwin. If ``kill_running``, flip every kill
        flag first so the genomes write a clean final audit record."""
        self._closed = True
        for h in list(self._darwins.values()):
            if kill_running and not h.killed and h.runtime.state.alive:
                h.killed = True
                h.stack.kill.flip("supervisor shutdown")
        # Let each drive task observe the kill and exit.
        tasks = [h.task for h in self._darwins.values() if h.task is not None]
        for t in tasks:
            try:
                await asyncio.wait_for(t, timeout=2.0)
            except asyncio.TimeoutError:
                t.cancel()
            except Exception:
                pass

    # -- introspection ----------------------------------------------------
    def list_ids(self) -> list[str]:
        return list(self._darwins)

    @property
    def router(self) -> EscalationRouter:
        """Read-only handle for ops/snapshots. Mutating the router from
        outside the supervisor is supported but not recommended."""
        return self._router

    @property
    def monitor(self) -> Monitor:
        """Read-only handle for ops/snapshots."""
        return self._monitor

    async def alerts(
        self,
        darwin_id: str,
        *,
        min_severity: Severity | None = None,
        since: float | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self._get(darwin_id)
        rows = self._monitor.alerts_for(
            darwin_id, min_severity=min_severity, since=since, limit=limit
        )
        return [a.to_dict() for a in rows]

    async def fleet_alerts(
        self,
        *,
        min_severity: Severity | None = None,
        since: float | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._monitor.all_alerts(
            min_severity=min_severity, since=since, limit=limit
        )
        return [a.to_dict() for a in rows]

    # -- internals --------------------------------------------------------
    def _get(self, darwin_id: str) -> _Handle:
        h = self._darwins.get(darwin_id)
        if h is None:
            raise UnknownDarwin(darwin_id)
        return h

    def _status(self, h: _Handle) -> DarwinStatus:
        id_ = h.runtime.identity
        st = h.runtime.state
        return DarwinStatus(
            darwin_id=id_.darwin_id,
            generation=id_.generation,
            parent_id=id_.parent_id,
            l0_version=id_.l0_version,
            birth_ts=id_.birth_ts,
            spawned_at=h.spawned_at,
            alive=bool(st.alive),
            paused=bool(getattr(st, "paused", False)),
            killed=h.killed,
            quarantined=h.quarantine.active,
            quarantine_reason=h.quarantine.reason,
            death_reason=st.death_reason,
            balance_usdc=float(h.stack.wallet.balance_usdc()),
            tick=int(st.tick),
            days_elapsed=float(st.days_elapsed),
            open_escalations=len(
                self._router.for_darwin(h.darwin_id, include_resolved=False)
            ),
        )

    def _ancestry(self, darwin_id: str) -> list[str]:
        out: list[str] = []
        cur = self._darwins.get(darwin_id)
        seen: set[str] = set()
        while cur and cur.parent_id and cur.parent_id not in seen:
            out.append(cur.parent_id)
            seen.add(cur.parent_id)
            cur = self._darwins.get(cur.parent_id)
        return out

    async def _drive(self, handle: _Handle) -> None:
        """Drive one Darwin: one heartbeat per loop iteration, with an
        ``asyncio.sleep`` between to yield to other Darwins on the same loop.
        Exits when the runtime stops being alive (death or kill)."""
        runtime = handle.runtime
        try:
            while runtime.state.alive:
                if handle.reproduce_requested:
                    # Set the genome's force-bypass flag rather than
                    # ``reproduce`` directly — ``check_reproduction`` is
                    # the gate and would otherwise overwrite the flag in
                    # the same heartbeat (and a forced amendment update
                    # has to actually fire, not get gated by fitness).
                    runtime.state.force_reproduce = True
                    handle.reproduce_requested = False
                await runtime.run(max_heartbeats=1)
                # A new child may have been spawned during this tick. Auto-
                # boot it so the same supervisor instance drives the next
                # generation — this is what a real platform's supervisor does
                # in response to its signing endpoint being called.
                self._observe_children(handle)
                # And any new tickets the genome posted during this tick get
                # classified + notified by the platform router.
                self._observe_escalations(handle)
                # The monitor records tick freshness on every cycle but only
                # runs the detectors every N cycles to keep the heartbeat
                # boundary cheap. Auto-actions are opt-in.
                handle.drive_cycles += 1
                self._monitor.observe_tick(handle.darwin_id, runtime.state.tick)
                if handle.drive_cycles % self._monitor_every_n == 0:
                    self._run_monitor(handle)
                if not runtime.state.alive:
                    break
                await asyncio.sleep(self._heartbeat)
        except asyncio.CancelledError:
            # Best-effort: try to halt cleanly. The kill flag may already be
            # set; if not, set it so the genome anchors a final record on its
            # next drive if the supervisor restarts.
            if not handle.killed:
                handle.killed = True
                handle.stack.kill.flip("supervisor cancelled")
            raise

    def _observe_children(self, parent: _Handle) -> None:
        spawner = parent.stack.spawner
        new = spawner.children[parent.children_observed :]
        if not new:
            return
        for record in new:
            try:
                manifest_data = json.loads(record["child_manifest"])
                child_id = manifest_data["identity"]["darwin_id"]
            except (KeyError, json.JSONDecodeError, TypeError):
                # A spawner record we cannot parse is a real bug, but the
                # supervisor must not crash because of it — record nothing
                # rather than die and take its siblings with it.
                continue
            parent.children.append(child_id)
            self._lineage.append(
                LineageEdge(
                    parent_id=parent.darwin_id,
                    child_id=child_id,
                    spawned_at=float(record.get("spawned_at", self._clock())),
                    budget_usdc=float(record.get("budget_usdc", 0.0)),
                )
            )
            # Boot the child if we have not already (same supervisor may have
            # seen it via a different path in future expansions).
            if child_id not in self._darwins:
                self._boot_child(parent, record, child_id)
        parent.children_observed = len(spawner.children)
        # doc 02: after a forced amendment reproduction, the parent dies.
        # We flip the kill flag here so the genome's next node-boundary
        # poll runs the clean halt sequence with audit + final anchor.
        if parent.kill_after_reproduction_reason and not parent.killed:
            with span(
                "supervisor.kill_after_amendment",
                darwin_id=parent.darwin_id,
                attributes={"reason": parent.kill_after_reproduction_reason},
            ) as s:
                annotate_span_with_runtime(s, parent.runtime)
                parent.killed = True
                try:
                    parent.stack.kill.flip(parent.kill_after_reproduction_reason)
                except Exception:
                    parent.killed = False
                parent.kill_after_reproduction_reason = None

    def _signals_for(self, handle: _Handle) -> DarwinSignals:
        rt = handle.runtime
        st = rt.state
        # Ticket open-timestamps from the router so escalation-rate detection
        # uses the *platform* view (the genome's pending_escalations list is
        # state-tick-indexed, not wall-clock).
        opened = tuple(
            t.opened_ts for t in self._router.all_for_darwin(handle.darwin_id)
        )
        return DarwinSignals(
            darwin_id=handle.darwin_id,
            now=self._clock(),
            alive=bool(st.alive),
            paused=bool(getattr(st, "paused", False)),
            state_tick=int(st.tick),
            last_tick_advance_ts=self._monitor.last_tick_advance(handle.darwin_id),
            movements=tuple(handle.stack.wallet.movements()),
            dispatches_done=int(st.dispatches_done),
            successful_dispatches=int(st.successful_dispatches),
            recurring_failures=dict(st.recurring_failures),
            ticket_opened_ts=opened,
        )

    def _run_monitor(self, handle: _Handle) -> None:
        """Build a signals snapshot, run detectors, optionally auto-pause on
        a fresh CRITICAL. Auto-pause is gated by ``auto_pause_on_critical``
        so a misconfigured quota cannot freeze the fleet."""
        try:
            signals = self._signals_for(handle)
        except Exception:
            return
        fired = self._monitor.check(signals)
        # Only open a span when alerts actually fired — keeps the trace
        # signal-to-noise high (the monitor runs every N ticks).
        if fired:
            with span(
                "supervisor.alerts",
                darwin_id=handle.darwin_id,
                attributes={"count": len(fired)},
            ) as s:
                annotate_span_with_runtime(s, handle.runtime)
                s.set_attribute(
                    "kinds", ",".join(sorted({a.kind for a in fired}))
                )
                s.set_attribute(
                    "max_severity",
                    max(a.severity.value for a in fired)
                    if fired
                    else "",
                )
        if handle.killed or not handle.runtime.state.alive:
            return
        # Auto-throttle on WARN (doc 09 level 2). Idempotent per (darwin,
        # kind) — once a throttle is in place the breach stops firing or
        # the operator clears the throttle explicitly.
        if self._auto_throttle_on_warn:
            self._maybe_auto_throttle(handle, fired)
        # Auto-pause on CRITICAL (doc 09 level 3). Pause once per kind so a
        # sustained breach doesn't re-pause an already-paused Darwin forever.
        # The Monitor's dedup already throttles sink emission; this gate is
        # idempotent per (darwin, kind) and clears on resume.
        if not self._auto_pause_on_critical:
            return
        for alert in fired:
            if alert.severity is not Severity.CRITICAL:
                continue
            if handle.auto_paused_for == alert.kind:
                continue
            with span(
                "supervisor.auto_pause",
                darwin_id=handle.darwin_id,
                attributes={"kind": alert.kind},
            ) as s:
                annotate_span_with_runtime(s, handle.runtime)
                handle.auto_paused_for = alert.kind
                try:
                    handle.stack.pause.pause(f"auto: {alert.kind} critical")
                except Exception:
                    handle.auto_paused_for = None
            break

    # Mapping of alert kind → the LLM tier we throttle on a WARN of that
    # kind. The matching tier is the most common driver of that signal:
    # high LLM-tier cost shows up first as spend_velocity; high coding-tier
    # use shows up as failure_rate (the soma is over-mutating). Other
    # kinds (heartbeat, escalation_rate) have no obvious tier mapping so
    # they don't auto-throttle — they just alert.
    _WARN_TIER_MAP = {
        "spend_velocity": "reasoning",
        "failure_rate": "coding",
    }

    def _maybe_auto_throttle(self, handle: _Handle, fired: list[Alert]) -> None:
        from darwin_dna.machinery.llm_router import Tier, hp_cap_for_tier

        for alert in fired:
            if alert.severity is not Severity.WARN:
                continue
            tier_name = self._WARN_TIER_MAP.get(alert.kind)
            if tier_name is None:
                continue
            if alert.kind in handle.auto_throttled:
                continue
            try:
                tier = Tier(tier_name)
            except ValueError:
                continue
            hp_cap = hp_cap_for_tier(handle.runtime._hp, tier)  # noqa: SLF001
            if hp_cap is None:
                continue
            # Halve the hp cap as the throttle. Already-spent calls stay
            # spent; ``remaining`` will take the min of (hp - spent,
            # throttle - spent).
            new_cap = max(1, hp_cap // 2)
            with span(
                "supervisor.auto_throttle",
                darwin_id=handle.darwin_id,
                attributes={
                    "kind": alert.kind,
                    "tier": tier_name,
                    "max_daily_calls": new_cap,
                },
            ) as s:
                annotate_span_with_runtime(s, handle.runtime)
                try:
                    handle.runtime.llm.set_throttle(tier, new_cap)
                    handle.auto_throttled.add(alert.kind)
                except Exception:
                    pass

    def _observe_escalations(self, handle: _Handle) -> None:
        """Sweep new tickets the genome posted to its inbox since last
        heartbeat through the router (classify, deadline, notify). Idempotent
        on ticket id, so a slow sink that fails to notify the first time
        gets another chance on the next sweep without re-firing for the
        ones that did notify."""
        tickets = handle.stack.escalation.tickets
        new = tickets[handle.escalation_observed :]
        if not new:
            return
        for payload in new:
            # The genome's posts include darwin_id; defensively inject the
            # handle's id when a test or other caller posts a payload without
            # one, so the router buckets correctly either way.
            if not payload.get("darwin_id"):
                payload = {**payload, "darwin_id": handle.darwin_id}
            with span(
                "escalation.register",
                darwin_id=handle.darwin_id,
                attributes={
                    "ticket_id": payload.get("ticket"),
                    "ticket_class": payload.get("ticket_class"),
                    "neuron": payload.get("neuron"),
                },
            ) as s:
                annotate_span_with_runtime(s, handle.runtime)
                try:
                    self._router.register(payload)
                except Exception:
                    # A malformed payload is a bug in whatever posted it.
                    # The supervisor must not die because of one bad
                    # ticket; skip it and let the others through.
                    continue
        handle.escalation_observed = len(tickets)

    def _boot_child(self, parent: _Handle, record: dict, child_id: str) -> None:
        with span(
            "supervisor.boot_child",
            darwin_id=child_id,
            attributes={
                "parent_id": parent.darwin_id,
                "budget_usdc": float(record.get("budget_usdc", 0.0)),
            },
        ) as s:
            try:
                child_platform, child_stack = make_child_platform(
                    signer=parent.stack.signer,
                    child_manifest_json=record["child_manifest"],
                    child_layer_blobs=record["child_layer_blobs"],
                    opening_usdc=float(record["budget_usdc"]),
                    clock=self._clock,
                )
                child_runtime = boot(child_platform, sleep=lambda _: None)
            except Exception:
                # A child whose DNA fails to verify is the platform's
                # problem, not the parent's — log nothing here (caller has
                # audit context), surface via lineage edges and absence
                # from _darwins.
                return
            annotate_span_with_runtime(s, child_runtime)
            child_handle = _Handle(
                darwin_id=child_id,
                stack=child_stack,
                runtime=child_runtime,
                spawned_at=self._clock(),
                parent_id=parent.darwin_id,
            )
            self._darwins[child_id] = child_handle
            child_handle.task = asyncio.create_task(
                self._drive(child_handle), name=f"darwin:{child_id}"
            )
