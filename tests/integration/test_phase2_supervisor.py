"""End-to-end Phase 2 wiring tests (docs/engineering/22).

The supervisor accepts five optional Phase 2 services. None of them
break the single-tenant dev path (each is None by default); when
wired, the supervisor exposes them via accessors + uses them in its
periodic hooks.
"""

from __future__ import annotations

import pytest

from darwin_platform.drift_detector import DailyProfile, DriftDetector
from darwin_platform.kyc import KYCService, StubKYCProvider
from darwin_platform.merkle_anchor import (
    MerkleAnchorPublisher,
    StubAnchorBackend,
)
from darwin_platform.namespace_provisioner import (
    NamespaceProvisioner,
    StubK8sBackend,
    StubOrganismDbBackend,
    StubVaultNamespaceBackend,
)
from darwin_platform.omnibus_wallet import OmnibusWallet
from darwin_platform.supervisor import Supervisor


@pytest.fixture
def _omnibus_chain():
    """In-process chain stub for the omnibus."""
    from tests.unit.darwin_platform.core.test_omnibus_wallet import _Chain
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
    s1 = await sup.spawn(opening_usdc=100.0, actor="test")
    s2 = await sup.spawn(opening_usdc=50.0, actor="test")
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
