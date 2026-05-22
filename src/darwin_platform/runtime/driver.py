"""Driver Protocol for per-Darwin lifecycle (docs/engineering/08).

A *driver* is whatever owns a running Darwin between :meth:`start` and
:meth:`stop`. Two implementations:

- :class:`InProcessDriver` (today's path) — every Darwin lives as an
  ``asyncio.Task`` inside the supervisor process. The implementation lives
  inside :mod:`darwin_platform.supervisor` because it is deeply
  intertwined with the supervisor's in-memory state (billing sweep,
  monitor poll, harvest scheduler, etc.). The supervisor IS the driver
  in this mode.
- :class:`K8sDarwinDriver` (this module's sibling, ``k8s.driver``) — each
  Darwin lives in its own ``darwin-{id}`` namespace, behind a
  ``runtimeClassName: kata`` microVM. The supervisor manages K8s
  resources via the kubernetes API and drives lifecycle over HTTP RPC
  to the per-Darwin orchestrator pod.

The Protocol is intentionally narrow: just the operator-facing lifecycle
verbs. Cross-cutting work (billing aggregation, monitor evaluation,
audit chain) stays on the supervisor and reads from shared Postgres in
K8s mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class DarwinSpec:
    """What the driver needs to start one Darwin.

    Keep this serializable — the K8s driver projects it into a ConfigMap
    that the per-Darwin orchestrator pod mounts at boot.
    """

    darwin_id: str
    opening_usdc: float
    escalation_auto: str | None = "approve"
    parent_id: str | None = None
    # Sealed DNA. The K8s driver writes these into a ConfigMap so the
    # orchestrator pod can reconstruct the same identity on boot (lossless
    # resume parity). The in-process driver ignores them — it mints
    # genesis through the FakeStack directly.
    manifest_json: str | None = None
    layer_blobs: dict[str, bytes] = field(default_factory=dict)
    layer_names: list[str] = field(default_factory=list)
    # Free-form passthrough — e.g. extra env vars or feature flags the
    # orchestrator pod should honour. Avoid putting secrets here; use the
    # K8s driver's per-Darwin Secret instead.
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DarwinSnapshot:
    """Lightweight status the driver can return without touching the
    runtime's full state. The supervisor's :class:`DarwinStatus` is a
    richer view built from this plus platform-side bookkeeping (open
    escalations, lineage edges, etc.)."""

    darwin_id: str
    alive: bool
    paused: bool
    killed: bool
    tick: int
    days_elapsed: float
    balance_usdc: float
    death_reason: str | None = None


@runtime_checkable
class DarwinDriver(Protocol):
    """The narrow per-Darwin lifecycle surface. Each method is async so a
    K8s driver can await API calls without blocking the supervisor's
    event loop."""

    async def start(self, spec: DarwinSpec) -> DarwinSnapshot:
        """Start the Darwin. After this returns, the Darwin is alive and
        the supervisor can route subsequent commands to it."""

    async def stop(self, darwin_id: str) -> None:
        """Tear down everything start() created. Idempotent: a second
        call on a stopped Darwin must not raise. Distinct from
        :meth:`kill` — stop is supervisor shutdown / GC; kill is a
        recorded death."""

    async def pause(self, darwin_id: str, *, reason: str) -> None:
        ...

    async def resume(self, darwin_id: str) -> None:
        ...

    async def kill(self, darwin_id: str, *, reason: str) -> None:
        ...

    async def force_reproduce(
        self,
        darwin_id: str,
        *,
        amendment_blob: bytes | None = None,
        amendment_name: str | None = None,
        kill_parent_reason: str | None = None,
    ) -> None:
        ...

    async def snapshot(self, darwin_id: str) -> DarwinSnapshot:
        """Read-only status. The supervisor calls this when answering
        ``GET /darwins/{id}`` and to detect death-by-natural-causes for
        K8s Darwins (a pod that reports alive=False has died and the
        driver should be told to ``stop`` it)."""

    async def list_ids(self) -> list[str]:
        """All Darwins this driver currently considers running. The
        supervisor cross-references this with its own bookkeeping at
        startup to reconcile state."""


class DriverError(Exception):
    """Driver-side rejection. The supervisor wraps these into
    :class:`darwin_platform.supervisor.SupervisorError` at the API edge."""


class DarwinNotFound(DriverError):
    """The driver has no record of this Darwin. Distinct from "alive but
    not responding" — that surfaces as a ``DriverError`` with cause."""

    def __init__(self, darwin_id: str) -> None:
        super().__init__(f"driver has no Darwin {darwin_id!r}")
        self.darwin_id = darwin_id
