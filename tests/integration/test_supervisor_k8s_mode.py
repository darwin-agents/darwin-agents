"""Supervisor.spawn() routes through the K8s driver when one is wired.

This test uses an in-memory fake driver — no real kubernetes API
interaction. It locks in the contract: spawn → driver.start, pause →
driver.pause, status → driver.snapshot.
"""

from __future__ import annotations

import pytest

from darwin_platform.runtime.driver import DarwinSnapshot, DarwinSpec
from darwin_platform.runtime.supervisor import Supervisor


class _FakeK8sDriver:
    def __init__(self):
        self.started: list[DarwinSpec] = []
        self.paused: list[tuple[str, str]] = []
        self.resumed: list[str] = []
        self.killed: list[tuple[str, str]] = []
        self.forced: list[str] = []
        self.snapshots_called: list[str] = []
        self.events_called: list[str] = []

    async def start(self, spec: DarwinSpec) -> DarwinSnapshot:
        self.started.append(spec)
        return DarwinSnapshot(
            darwin_id=spec.darwin_id, alive=True, paused=False, killed=False,
            tick=0, days_elapsed=0.0, balance_usdc=spec.opening_usdc,
        )

    async def stop(self, darwin_id: str) -> None: ...

    async def pause(self, darwin_id, *, reason): self.paused.append((darwin_id, reason))

    async def resume(self, darwin_id): self.resumed.append(darwin_id)

    async def kill(self, darwin_id, *, reason): self.killed.append((darwin_id, reason))

    async def force_reproduce(self, darwin_id, *, amendment_blob=None,
                              amendment_name=None, kill_parent_reason=None):
        self.forced.append(darwin_id)

    async def snapshot(self, darwin_id: str) -> DarwinSnapshot:
        self.snapshots_called.append(darwin_id)
        return DarwinSnapshot(
            darwin_id=darwin_id, alive=True, paused=False, killed=False,
            tick=42, days_elapsed=1.5, balance_usdc=99.0,
        )

    async def events_records(self, darwin_id: str) -> list[dict]:
        self.events_called.append(darwin_id)
        return [
            {"seq": 1, "ts": 1.0, "kind": "event:spawn",
             "darwin_id": darwin_id, "payload": {}},
            {"seq": 2, "ts": 2.0, "kind": "event:tick",
             "darwin_id": darwin_id, "payload": {"tier": "fast"}},
        ]

    async def list_ids(self) -> list[str]: return []


@pytest.mark.asyncio
async def test_spawn_in_k8s_mode_goes_through_driver():
    driver = _FakeK8sDriver()
    sup = Supervisor(k8s_driver=driver, heartbeat_seconds=0.0)
    status = await sup.spawn(opening_usdc=100.0, escalation_auto="approve")
    assert len(driver.started) == 1
    assert driver.started[0].opening_usdc == 100.0
    # The supervisor records the new id in its own bookkeeping so
    # subsequent lifecycle calls route correctly.
    assert status.darwin_id in sup.list_ids()
    assert status.alive is True


@pytest.mark.asyncio
async def test_pause_resume_kill_route_through_driver():
    driver = _FakeK8sDriver()
    sup = Supervisor(k8s_driver=driver, heartbeat_seconds=0.0)
    status = await sup.spawn(opening_usdc=100.0)
    did = status.darwin_id
    await sup.pause(did, reason="op")
    await sup.resume(did)
    await sup.kill(did, reason="op")
    await sup.force_reproduce(did)
    assert driver.paused == [(did, "op")]
    assert driver.resumed == [did]
    assert driver.killed == [(did, "op")]
    assert driver.forced == [did]


@pytest.mark.asyncio
async def test_status_reads_driver_snapshot():
    driver = _FakeK8sDriver()
    sup = Supervisor(k8s_driver=driver, heartbeat_seconds=0.0)
    status = await sup.spawn(opening_usdc=100.0)
    fresh = await sup.status(status.darwin_id)
    # Snapshot from the fake driver carries tick=42 — it must surface
    # back through the supervisor's status.
    assert fresh.tick == 42
    assert fresh.balance_usdc == 99.0


@pytest.mark.asyncio
async def test_shutdown_kills_each_k8s_darwin():
    driver = _FakeK8sDriver()
    sup = Supervisor(k8s_driver=driver, heartbeat_seconds=0.0)
    a = (await sup.spawn(opening_usdc=10.0)).darwin_id
    b = (await sup.spawn(opening_usdc=10.0)).darwin_id
    await sup.shutdown(kill_running=True)
    killed_ids = {did for did, _r in driver.killed}
    assert {a, b} <= killed_ids


@pytest.mark.asyncio
async def test_events_route_through_driver_in_k8s_mode():
    """events() must fetch from the driver (orchestrator pod), not the
    in-process audit sink, and still apply filtering/paging."""
    driver = _FakeK8sDriver()
    sup = Supervisor(k8s_driver=driver, heartbeat_seconds=0.0)
    status = await sup.spawn(opening_usdc=100.0)
    did = status.darwin_id

    page = await sup.events(did, limit=100)
    assert did in driver.events_called
    assert page["darwin_id"] == did
    assert len(page["events"]) == 2

    # Filtering composes on the driver-fetched records.
    only_fast = await sup.events(did, tier="fast")
    assert len(only_fast["events"]) == 1
    assert only_fast["events"][0]["kind"] == "event:tick"


@pytest.mark.asyncio
async def test_alerts_do_not_404_in_k8s_mode():
    """alerts() must not raise UnknownDarwin for a k8s darwin (returns []
    until orchestrator-side alert propagation lands)."""
    driver = _FakeK8sDriver()
    sup = Supervisor(k8s_driver=driver, heartbeat_seconds=0.0)
    status = await sup.spawn(opening_usdc=100.0)
    rows = await sup.alerts(status.darwin_id)
    assert rows == []
