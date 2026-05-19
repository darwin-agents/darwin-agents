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
    death_reason: str | None
    balance_usdc: float
    tick: int
    days_elapsed: float
    open_escalations: int


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
    ) -> None:
        # Loop pacing: between heartbeats the supervisor's drive task awaits
        # for this long. Default tiny — tests run as fast as the event loop;
        # the API runner can override.
        self._heartbeat = float(heartbeat_seconds)
        self._clock = clock
        self._darwins: dict[str, _Handle] = {}
        self._lineage: list[LineageEdge] = []
        self._closed = False

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

        stack = FakeStack(
            opening_usdc=opening_usdc,
            clock=self._clock,
            escalation_auto=escalation_auto,
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
        h = self._get(darwin_id)
        if h.killed:
            raise SupervisorError(f"darwin {darwin_id} is killed; pause is a no-op")
        h.stack.pause.pause(reason)

    async def resume(self, darwin_id: str) -> None:
        h = self._get(darwin_id)
        if h.killed:
            raise SupervisorError(f"darwin {darwin_id} is killed; cannot resume")
        h.stack.pause.resume()

    # -- kill (doc 08: POST /darwins/{id}/kill) ---------------------------
    async def kill(self, darwin_id: str, *, reason: str = "owner kill") -> None:
        """Flip the platform kill flag. The genome polls it at the loop
        boundary and at every node boundary (doc 09: ≤5s latency), then runs
        ``_halt_on_kill`` which writes the final audit record and anchors."""
        h = self._get(darwin_id)
        h.killed = True
        h.stack.kill.flip(reason)

    # -- force reproduction (doc 08: POST /darwins/{id}/reproduce/force) --
    async def force_reproduce(self, darwin_id: str) -> None:
        """Set the genome's reproduce flag for the next heartbeat. The
        genome still runs its own treasury/fitness gates inside ``_reproduce``;
        forcing requests, it does not bypass."""
        h = self._get(darwin_id)
        if not h.runtime.state.alive:
            raise SupervisorError(f"darwin {darwin_id} is not alive")
        h.reproduce_requested = True

    # -- escalations (doc 08: POST .../escalations/{eid}/respond) ---------
    async def respond_escalation(
        self,
        darwin_id: str,
        *,
        escalation_id: str,
        decision: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Route an owner decision to the genome's escalation inbox."""
        if decision not in ("approve", "deny", "data"):
            raise SupervisorError(f"unknown decision: {decision}")
        h = self._get(darwin_id)
        h.stack.escalation.respond(escalation_id, decision, data or {})
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
    async def open_escalations(self, darwin_id: str) -> list[dict[str, Any]]:
        """The tickets posted by the genome that the supervisor has not yet
        responded to. Lets a real owner UI render the inbox."""
        h = self._get(darwin_id)
        return [
            dict(t)
            for t in h.stack.escalation.tickets
            if t.get("ticket") and t["ticket"] not in h.handled_responses
        ]

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
            death_reason=st.death_reason,
            balance_usdc=float(h.stack.wallet.balance_usdc()),
            tick=int(st.tick),
            days_elapsed=float(st.days_elapsed),
            open_escalations=len(
                [
                    t
                    for t in h.stack.escalation.tickets
                    if t.get("ticket") and t["ticket"] not in h.handled_responses
                ]
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
                    runtime.state.reproduce = True
                    handle.reproduce_requested = False
                await runtime.run(max_heartbeats=1)
                # A new child may have been spawned during this tick. Auto-
                # boot it so the same supervisor instance drives the next
                # generation — this is what a real platform's supervisor does
                # in response to its signing endpoint being called.
                self._observe_children(handle)
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

    def _boot_child(self, parent: _Handle, record: dict, child_id: str) -> None:
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
            # A child whose DNA fails to verify is the platform's problem,
            # not the parent's — log nothing here (caller has audit context),
            # surface via lineage edges and absence from _darwins.
            return
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
