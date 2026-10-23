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
        self.reconcile_result: list[dict] = []

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
        # The orchestrator stamps records with the genome's own genesis
        # identity, which is independent of the supervisor handle id passed
        # here. Use a deliberately *different* id so the test proves the
        # supervisor does NOT drop them via a darwin_id equality filter.
        genesis_id = "019e870a-genesis-id"
        assert genesis_id != darwin_id
        return [
            {"seq": 1, "ts": 1.0, "kind": "event:spawn",
             "darwin_id": genesis_id, "payload": {}},
            {"seq": 2, "ts": 2.0, "kind": "event:tick",
             "darwin_id": genesis_id, "payload": {"tier": "fast"}},
        ]

    async def list_ids(self) -> list[str]: return []

    async def reconcile(self) -> list[dict]:
        return list(self.reconcile_result)


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
async def test_reconcile_readopts_k8s_darwins_on_restart():
    """A fresh supervisor (in-memory _k8s_handles empty) re-adopts darwins the
    driver still sees running, so they route as k8s + answer status again —
    instead of 404ing while their orchestrators keep spending."""
    driver = _FakeK8sDriver()
    driver.reconcile_result = [
        {"darwin_id": "d-resumed", "spawned_at": 100.0,
         "parent_id": "d-parent", "opening_usdc": 50.0},
    ]
    sup = Supervisor(k8s_driver=driver, heartbeat_seconds=0.0)

    # Before reconcile the supervisor knows nothing about it.
    assert "d-resumed" not in sup.list_ids()

    await sup._reconcile_k8s()

    # After reconcile it routes as a k8s darwin and status works.
    assert "d-resumed" in sup.list_ids()
    assert sup._is_k8s("d-resumed")
    status = await sup.status("d-resumed")
    assert status.darwin_id == "d-resumed"
    assert "d-resumed" in driver.snapshots_called


@pytest.mark.asyncio
async def test_alerts_do_not_404_in_k8s_mode():
    """alerts() must not raise UnknownDarwin for a k8s darwin (returns []
    until orchestrator-side alert propagation lands)."""
    driver = _FakeK8sDriver()
    sup = Supervisor(k8s_driver=driver, heartbeat_seconds=0.0)
    status = await sup.spawn(opening_usdc=100.0)
    rows = await sup.alerts(status.darwin_id)
    assert rows == []


class _ForensicsK8sDriver(_FakeK8sDriver):
    """Adds the pod-side forensics control verb the kill path pulls."""

    async def forensics(self, darwin_id: str) -> dict:
        return {
            "schema": "darwin-platform.soma-forensic-k8s/1",
            "snapshot_id": "death-4242",
            "darwin_id": darwin_id,
            "reason": "owner kill",
            "soma_digest": "feedface",
            "file_count": 4,
            "layers": [{"index": 0, "name": "l0_genome", "sha256": "ab"}],
        }


@pytest.mark.asyncio
async def test_kill_persists_pod_forensics_for_retrieval(tmp_path):
    """On kill, the supervisor pulls the pod's death-time code fingerprint and
    persists it so list/read_soma_forensic surface k8s deaths like in-process
    ones."""
    driver = _ForensicsK8sDriver()
    sup = Supervisor(
        k8s_driver=driver, heartbeat_seconds=0.0, soma_root=tmp_path,
    )
    status = await sup.spawn(opening_usdc=100.0)
    did = status.darwin_id
    await sup.kill(did, reason="owner kill", actor="alice")

    rows = sup.list_soma_forensics(did)
    assert len(rows) == 1
    assert rows[0]["snapshot_id"] == "death-4242"
    assert rows[0]["soma_digest"] == "feedface"

    full = sup.read_soma_forensic(did, "death-4242")
    assert full["layers"][0]["name"] == "l0_genome"

    audit = sup.audit_log.entries(action="soma_forensic_snapshot")
    assert audit and audit[-1].metadata["driver"] == "k8s"
    assert audit[-1].metadata["captured"] is True

    # Idempotent: a double-kill must not write a duplicate snapshot.
    await sup.kill(did, reason="owner kill again", actor="alice")
    assert len(sup.list_soma_forensics(did)) == 1


class _DeadPodK8sDriver(_FakeK8sDriver):
    """A pod that reports it has died of bankruptcy + serves its fingerprint."""

    async def snapshot(self, darwin_id: str) -> DarwinSnapshot:
        return DarwinSnapshot(
            darwin_id=darwin_id, alive=False, paused=False, killed=False,
            tick=9, days_elapsed=2.0, balance_usdc=0.0,
            death_reason="bankruptcy: broke",
        )

    async def forensics(self, darwin_id: str) -> dict:
        return {
            "schema": "darwin-platform.soma-forensic-k8s/1",
            "snapshot_id": "death-7777", "darwin_id": darwin_id,
            "reason": "bankruptcy: broke", "soma_digest": "cafe",
            "file_count": 2,
        }


@pytest.mark.asyncio
async def test_spawn_sets_soma_enabled_when_soma_root_configured(tmp_path):
    """A supervisor configured with a soma root tells the pod to run real
    soma-merge (so its forensics capture file contents, not just layers)."""
    driver = _FakeK8sDriver()
    sup = Supervisor(
        k8s_driver=driver, heartbeat_seconds=0.0, soma_root=tmp_path,
    )
    await sup.spawn(opening_usdc=100.0)
    assert driver.started[0].soma_enabled is True

    # No soma root → simulated (today's behavior).
    driver2 = _FakeK8sDriver()
    sup2 = Supervisor(k8s_driver=driver2, heartbeat_seconds=0.0)
    await sup2.spawn(opening_usdc=100.0)
    assert driver2.started[0].soma_enabled is False


@pytest.mark.asyncio
async def test_k8s_natural_death_captured_by_maintenance(tmp_path):
    """A k8s Darwin that dies of bankruptcy (not an operator kill) is captured
    by the fleet-maintenance loop, which observes the not-alive snapshot and
    pulls + persists the pod's fingerprint before reap (doc 05)."""
    driver = _DeadPodK8sDriver()
    sup = Supervisor(
        k8s_driver=driver, heartbeat_seconds=0.0, soma_root=tmp_path,
    )
    status = await sup.spawn(opening_usdc=100.0)
    did = status.darwin_id
    await sup._run_k8s_fleet_maintenance(sup._clock())  # noqa: SLF001
    rows = sup.list_soma_forensics(did)
    assert len(rows) == 1
    assert rows[0]["snapshot_id"] == "death-7777"
    # Idempotent across maintenance cycles.
    await sup._run_k8s_fleet_maintenance(sup._clock())  # noqa: SLF001
    assert len(sup.list_soma_forensics(did)) == 1


@pytest.mark.asyncio
async def test_kill_without_forensics_verb_is_noop(tmp_path):
    """A driver without a forensics verb (older driver) must not break kill."""
    driver = _FakeK8sDriver()  # no .forensics
    sup = Supervisor(
        k8s_driver=driver, heartbeat_seconds=0.0, soma_root=tmp_path,
    )
    status = await sup.spawn(opening_usdc=100.0)
    did = status.darwin_id
    await sup.kill(did, reason="op")  # must not raise
    assert sup.list_soma_forensics(did) == []


@pytest.mark.asyncio
async def test_k8s_spawn_seals_watch_wallet_substrate_without_signing_keys():
    """Each pod gets the forwarded substrate plus a watch-only wallet on its
    own Darwin's address; the signing account stays in the supervisor."""
    import json

    class _Signer:
        def __init__(self, darwin_id):
            self.darwin_id = darwin_id

        def address(self):
            return "0x" + self.darwin_id[:8].rjust(40, "0")

    driver = _FakeK8sDriver()
    sup = Supervisor(
        k8s_driver=driver, heartbeat_seconds=0.0,
        pod_substrate={"network": "testnet", "e2b_api_key": "e2b-x"},
        pod_signer_factory=lambda did, clock: _Signer(did),
    )
    status = await sup.spawn(opening_usdc=10.0)
    sub = json.loads(driver.started[0].secret_extras["substrate.json"])
    assert sub["wallet_provider"] == "watch"
    assert sub["wallet_address"] == sup._pod_signer(status.darwin_id).address()  # noqa: SLF001
    assert sub["wallet_state_dir"] == "/data/wallet"
    assert sub["e2b_api_key"] == "e2b-x"
    assert not any(k.startswith("cdp") or k == "wallet_key" for k in sub)
