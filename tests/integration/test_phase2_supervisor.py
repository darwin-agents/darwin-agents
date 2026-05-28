"""End-to-end Phase 2 wiring tests (docs/engineering/22).

The supervisor accepts five optional Phase 2 services. None of them
break the single-tenant dev path (each is None by default); when
wired, the supervisor exposes them via accessors + uses them in its
periodic hooks.
"""

from __future__ import annotations

import pytest

from darwin_platform.observability.drift_detector import DailyProfile, DriftDetector
from darwin_platform.security.kyc import KYCService, StubKYCProvider
from darwin_platform.observability.merkle_anchor import (
    MerkleAnchorPublisher,
    StubAnchorBackend,
)
from darwin_platform.tenancy.namespace_provisioner import (
    NamespaceProvisioner,
    StubK8sBackend,
    StubOrganismDbBackend,
    StubVaultNamespaceBackend,
)
from darwin_platform.payments.omnibus_wallet import OmnibusWallet
from darwin_platform.runtime.supervisor import Supervisor


@pytest.fixture
def _omnibus_chain():
    """In-process chain stub for the omnibus."""
    from tests.unit.darwin_platform.payments.test_omnibus_wallet import _Chain
    return _Chain(balance=10_000.0)


@pytest.mark.asyncio
async def test_supervisor_accepts_all_phase2_services(_omnibus_chain):
    sup = Supervisor(
        heartbeat_seconds=0.001,
        kyc_service=KYCService(provider=StubKYCProvider()),
        omnibus_wallet=OmnibusWallet(_omnibus_chain),
        namespace_provisioner=NamespaceProvisioner(
            k8s=StubK8sBackend(),
            vault=StubVaultNamespaceBackend(),
            organism_db=StubOrganismDbBackend(),
        ),
        drift_detector=DriftDetector(),
        merkle_anchor_publisher=MerkleAnchorPublisher(backend=StubAnchorBackend()),
    )
    try:
        assert sup.kyc_service is not None
        assert sup.omnibus_wallet is not None
        assert sup.namespace_provisioner is not None
        assert sup.drift_detector is not None
        assert sup.merkle_anchor is not None
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_wires_omnibus_address_into_spawner(_omnibus_chain):
    """Step c: when an OmnibusWallet is wired, the supervisor injects its
    on-chain address into stack.spawner so the parent's reproduction
    transfer settles to a real destination instead of the symbolic
    fallback. The default _omnibus_chain stub returns '0xomnibus'."""
    sup = Supervisor(
        heartbeat_seconds=0.001,
        omnibus_wallet=OmnibusWallet(_omnibus_chain),
    )
    try:
        status = await sup.spawn(opening_usdc=100.0)
        handle = sup._darwins[status.darwin_id]
        assert handle.stack.spawner._omnibus_destination == "0xomnibus"
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_spawner_destination_none_without_omnibus():
    """Without an omnibus the spawner keeps its symbolic fallback so the
    90-day sim + dev runs don't need any chain configuration."""
    sup = Supervisor(heartbeat_seconds=0.001)
    try:
        status = await sup.spawn(opening_usdc=100.0)
        handle = sup._darwins[status.darwin_id]
        assert handle.stack.spawner._omnibus_destination is None
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_provisions_tenant_namespace_on_spawn(_omnibus_chain):
    """When a NamespaceProvisioner is wired, _spawn calls provision() so
    the K8s namespace + vault namespace + organism DB land in lockstep
    with the Darwin's lifecycle."""
    k8s = StubK8sBackend()
    vault = StubVaultNamespaceBackend()
    db = StubOrganismDbBackend()
    sup = Supervisor(
        heartbeat_seconds=0.001,
        namespace_provisioner=NamespaceProvisioner(
            k8s=k8s, vault=vault, organism_db=db,
        ),
    )
    try:
        status = await sup.spawn(opening_usdc=100.0, actor="alice")
        # All three sides of the tenant got created.
        assert k8s.namespaces  # K8s ns created
        assert vault.namespaces  # Vault ns created
        assert db.databases  # organism DB created
        tenant = sup.namespace_provisioner.get(status.darwin_id)
        assert tenant is not None
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_tears_down_tenant_on_kill(_omnibus_chain):
    k8s = StubK8sBackend()
    vault = StubVaultNamespaceBackend()
    db = StubOrganismDbBackend()
    sup = Supervisor(
        heartbeat_seconds=0.001,
        namespace_provisioner=NamespaceProvisioner(
            k8s=k8s, vault=vault, organism_db=db,
        ),
    )
    try:
        status = await sup.spawn(opening_usdc=100.0, actor="alice")
        await sup.kill(status.darwin_id, reason="test", actor="alice")
        # All three sides got the delete call.
        assert k8s.deletions
        assert vault.deletions
        assert db.dropped
        assert sup.namespace_provisioner.get(status.darwin_id) is None
    finally:
        await sup.shutdown(kill_running=False)


@pytest.mark.asyncio
async def test_supervisor_relays_reproduction_to_child_via_omnibus(_omnibus_chain):
    """Step (c) second leg: with an omnibus wallet wired, the supervisor's
    _relay_reproduction_to_child helper submits a chain transfer from the
    omnibus to the child's per-Darwin wallet address. Exercises the helper
    directly so the test doesn't depend on FakeSpawner's reproduction +
    DNA-verification path (which has its own coverage)."""
    sup = Supervisor(
        heartbeat_seconds=0.001,
        omnibus_wallet=OmnibusWallet(_omnibus_chain),
    )
    try:
        parent_status = await sup.spawn(opening_usdc=100.0, actor="alice")
        child_status = await sup.spawn(opening_usdc=25.0, actor="alice")
        parent = sup._darwins[parent_status.darwin_id]
        child = sup._darwins[child_status.darwin_id]
        child_address = child.stack.wallet.address()
        # Fake the spawner record the supervisor would observe.
        record = {"budget_usdc": 25.0}
        sup._relay_reproduction_to_child(parent, child_status.darwin_id, record)

        relays = [
            e for e in sup.omnibus_wallet.ledger()
            if e.kind.value == "relay_reproduction"
        ]
        assert len(relays) == 1
        assert relays[0].counterparty == child_status.darwin_id
        assert relays[0].amount_usdc == pytest.approx(25.0)
        # Chain transfer landed at the child's wallet address, not at any
        # symbolic fallback.
        assert _omnibus_chain.transfers[-1]["to"] == child_address
        assert _omnibus_chain.transfers[-1]["amount_usdc"] == pytest.approx(25.0)
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_relay_failure_audited_not_raised(_omnibus_chain):
    """Chain failure on the second leg must be audit-logged but never
    crash the spawn-observation loop — a child can be alive even if the
    omnibus settlement hits a transient hiccup."""
    sup = Supervisor(
        heartbeat_seconds=0.001,
        omnibus_wallet=OmnibusWallet(_omnibus_chain),
    )
    try:
        parent_status = await sup.spawn(opening_usdc=100.0, actor="alice")
        child_status = await sup.spawn(opening_usdc=25.0, actor="alice")
        parent = sup._darwins[parent_status.darwin_id]
        # Force the next omnibus chain transfer to fail.
        _omnibus_chain.fail_next = True

        sup._relay_reproduction_to_child(
            parent, child_status.darwin_id, {"budget_usdc": 10.0},
        )
        # No relay landed; an audit entry did.
        relays = [
            e for e in sup.omnibus_wallet.ledger()
            if e.kind.value == "relay_reproduction"
        ]
        assert relays == []
        failures = sup.audit_log.entries(action="reproduction_relay_failed")
        assert len(failures) == 1
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_defaults_phase2_services_to_none():
    """Single-tenant dev path: nothing is wired by default."""
    sup = Supervisor(heartbeat_seconds=0.001)
    try:
        assert sup.kyc_service is None
        assert sup.omnibus_wallet is None
        assert sup.namespace_provisioner is None
        assert sup.drift_detector is None
        assert sup.merkle_anchor is None
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_collect_daily_merkle_roots_walks_alive_darwins():
    sup = Supervisor(heartbeat_seconds=0.001)
    s1 = await sup.spawn(opening_usdc=100.0, actor="test")
    s2 = await sup.spawn(opening_usdc=100.0, actor="test")
    try:
        roots = sup.collect_daily_merkle_roots("2026-05-21")
        assert {r.darwin_id for r in roots} == {s1.darwin_id, s2.darwin_id}
        # Each root is a valid sha256 hex string.
        for r in roots:
            assert len(r.root) == 64
            int(r.root, 16)  # hex
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_reconcile_omnibus_returns_none_when_no_omnibus():
    sup = Supervisor(heartbeat_seconds=0.001)
    try:
        assert sup.reconcile_omnibus() is None
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_reconcile_omnibus_sums_darwin_wallets(_omnibus_chain):
    sup = Supervisor(
        heartbeat_seconds=0.001,
        omnibus_wallet=OmnibusWallet(_omnibus_chain),
    )
    await sup.spawn(opening_usdc=100.0, actor="test")
    await sup.spawn(opening_usdc=50.0, actor="test")
    try:
        result = sup.reconcile_omnibus()
        assert result is not None
        # Two Darwins holding 150 USDC total.
        assert result.balances.sum_of_darwin_wallets == pytest.approx(150.0)
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_observe_drift_no_detector_returns_empty():
    sup = Supervisor(heartbeat_seconds=0.001)
    try:
        alerts = sup.observe_drift(DailyProfile(
            darwin_id="d-1", day_iso="2026-05-21",
            neuron_dispatches={}, spend_by_category={},
            escalations_by_class={}, total_dispatches=0, total_spend_usdc=0.0,
        ))
        assert alerts == []
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_observe_drift_with_detector_feeds_through():
    detector = DriftDetector(min_observations=2)
    sup = Supervisor(heartbeat_seconds=0.001, drift_detector=detector)
    try:
        for i in range(5):
            sup.observe_drift(DailyProfile(
                darwin_id="d-1", day_iso=f"2026-05-{i+1:02d}",
                neuron_dispatches={"content.publisher": 10},
                spend_by_category={}, escalations_by_class={},
                total_dispatches=10, total_spend_usdc=0.0,
            ))
        # The detector now has 5 days of history; spike fires.
        alerts = sup.observe_drift(DailyProfile(
            darwin_id="d-1", day_iso="2026-05-06",
            neuron_dispatches={"content.publisher": 200},
            spend_by_category={}, escalations_by_class={},
            total_dispatches=200, total_spend_usdc=0.0,
        ))
        assert len(alerts) > 0
    finally:
        await sup.shutdown(kill_running=True)
