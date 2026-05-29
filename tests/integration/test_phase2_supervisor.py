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
async def test_supervisor_darwin_fiat_payout_runs_both_legs(_omnibus_chain):
    """End-to-end: Darwin wallet debits to omnibus address (leg 1), omnibus
    submits fiat withdraw via ramp (leg 2), audit + ledger reflect both.
    Uses a freelancer registry entry to gate the destination."""
    from darwin_platform.adapters.fiat import StubFiatRamp
    from darwin_platform.payments.freelancer_registry import (
        InMemoryFreelancerRegistry,
    )

    ramp = StubFiatRamp()
    registry = InMemoryFreelancerRegistry()
    sup = Supervisor(
        heartbeat_seconds=0.001,
        omnibus_wallet=OmnibusWallet(_omnibus_chain),
        fiat_ramp=ramp,
        freelancer_registry=registry,
    )
    try:
        status = await sup.spawn(opening_usdc=200.0, actor="alice")
        wallet = sup._darwins[status.darwin_id].stack.wallet
        balance_before = wallet.balance_usdc()
        # Owner registers the freelancer before the payout.
        sup.register_freelancer(
            owner_subject="alice",
            freelancer_id="upwork:writer-42",
            fiat_destination="acct_freelancer_bank_1",
            display_name="Jane Writer",
            kyc_status="verified",
            actor="alice",
        )
        result = await sup.darwin_fiat_payout(
            status.darwin_id,
            amount_usdc=30.0,
            freelancer_id="upwork:writer-42",
            actor="alice",
        )
        # Leg 1: Darwin wallet debited by 30.
        assert wallet.balance_usdc() == pytest.approx(balance_before - 30.0)
        # Leg 2: omnibus ledger has a darwin_fiat_payout entry.
        entries = sup.omnibus_wallet.ledger()
        relays = [
            e for e in entries
            if e.kind.value == "darwin_fiat_payout"
        ]
        assert len(relays) == 1
        assert relays[0].counterparty == status.darwin_id
        assert relays[0].metadata["fiat_destination"] == "acct_freelancer_bank_1"
        assert result["kind"] == "darwin_fiat_payout"
        # Audit trail captures both the freelancer_id and resolved
        # destination + kyc_status — operator can reconstruct everything.
        audits = sup.audit_log.entries(action="darwin_fiat_payout")
        assert len(audits) == 1
        assert audits[0].metadata["freelancer_id"] == "upwork:writer-42"
        assert audits[0].metadata["kyc_status"] == "verified"
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_darwin_fiat_payout_requires_omnibus_and_ramp():
    from darwin_platform.payments.freelancer_registry import (
        InMemoryFreelancerRegistry,
    )

    sup = Supervisor(
        heartbeat_seconds=0.001,
        freelancer_registry=InMemoryFreelancerRegistry(),
    )
    try:
        status = await sup.spawn(opening_usdc=100.0, actor="alice")
        with pytest.raises(Exception, match="omnibus|fiat ramp"):
            await sup.darwin_fiat_payout(
                status.darwin_id,
                amount_usdc=10.0,
                freelancer_id="x",
                actor="alice",
            )
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_darwin_fiat_payout_refuses_unregistered_freelancer(
    _omnibus_chain,
):
    """Even with omnibus + ramp wired, a freelancer not in the registry
    for the owning user must be rejected before any chain transfer."""
    from darwin_platform.adapters.fiat import StubFiatRamp
    from darwin_platform.payments.freelancer_registry import (
        InMemoryFreelancerRegistry,
    )

    sup = Supervisor(
        heartbeat_seconds=0.001,
        omnibus_wallet=OmnibusWallet(_omnibus_chain),
        fiat_ramp=StubFiatRamp(),
        freelancer_registry=InMemoryFreelancerRegistry(),
    )
    try:
        status = await sup.spawn(opening_usdc=200.0, actor="alice")
        with pytest.raises(Exception, match="not registered"):
            await sup.darwin_fiat_payout(
                status.darwin_id,
                amount_usdc=10.0,
                freelancer_id="ghost",
                actor="alice",
            )
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_darwin_fiat_payout_isolates_owner_scope(_omnibus_chain):
    """Alice's registered freelancer must not be reachable from Bob's
    Darwin — registry is owner-scoped."""
    from darwin_platform.adapters.fiat import StubFiatRamp
    from darwin_platform.payments.freelancer_registry import (
        InMemoryFreelancerRegistry,
    )

    sup = Supervisor(
        heartbeat_seconds=0.001,
        omnibus_wallet=OmnibusWallet(_omnibus_chain),
        fiat_ramp=StubFiatRamp(),
        freelancer_registry=InMemoryFreelancerRegistry(),
    )
    try:
        bob_status = await sup.spawn(opening_usdc=200.0, actor="bob")
        sup.register_freelancer(
            owner_subject="alice",
            freelancer_id="freelancer-1",
            fiat_destination="acct_alice_friend",
            actor="alice",
        )
        with pytest.raises(Exception, match="not registered"):
            await sup.darwin_fiat_payout(
                bob_status.darwin_id,
                amount_usdc=10.0,
                freelancer_id="freelancer-1",
                actor="bob",
            )
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_register_revoke_list_freelancer_round_trip():
    from darwin_platform.payments.freelancer_registry import (
        InMemoryFreelancerRegistry,
    )

    sup = Supervisor(
        heartbeat_seconds=0.001,
        freelancer_registry=InMemoryFreelancerRegistry(),
    )
    try:
        sup.register_freelancer(
            owner_subject="alice",
            freelancer_id="f1",
            fiat_destination="acct_1",
            display_name="One",
            kyc_status="verified",
        )
        sup.register_freelancer(
            owner_subject="alice",
            freelancer_id="f2",
            fiat_destination="acct_2",
            display_name="Two",
        )
        listed = sup.list_freelancers(owner_subject="alice")
        assert [d["freelancer_id"] for d in listed] == ["f1", "f2"]
        result = sup.revoke_freelancer(
            owner_subject="alice", freelancer_id="f1",
        )
        assert result == {"revoked": True, "freelancer_id": "f1"}
        listed_after = sup.list_freelancers(owner_subject="alice")
        assert [d["freelancer_id"] for d in listed_after] == ["f2"]
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_pre_approval_round_trip():
    """Owner-set pre-approval lands on the Darwin's wallet, surfaces via
    list_preapprovals, and revoke clears it."""
    sup = Supervisor(heartbeat_seconds=0.001)
    try:
        status = await sup.spawn(opening_usdc=100.0, actor="alice")
        # No rules at start.
        assert await sup.list_preapprovals(status.darwin_id) == []
        # Install one.
        stored = await sup.set_preapproval(
            status.darwin_id,
            rule_id="upwork-under-50",
            max_amount_usdc=50.0,
            counterparty_category="freelancer",
            counterparty_substring="upwork",
            actor="alice",
        )
        assert stored["rule_id"] == "upwork-under-50"
        listed = await sup.list_preapprovals(status.darwin_id)
        assert len(listed) == 1
        assert listed[0]["rule_id"] == "upwork-under-50"
        # Revoke it.
        result = await sup.revoke_preapproval(
            status.darwin_id, "upwork-under-50", actor="alice",
        )
        assert result == {"revoked": True, "rule_id": "upwork-under-50"}
        assert await sup.list_preapprovals(status.darwin_id) == []
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_pre_approval_clears_freelancer_escalation():
    """End-to-end: with a matching pre-approval installed, a freelancer
    payment via payments.facilitator returns ok=True instead of
    escalating. Walks the policy engine through the facilitator's
    _state() pipeline using the wallet the supervisor wired."""
    from darwin_dna.neurons import Task

    sup = Supervisor(heartbeat_seconds=0.001)
    try:
        status = await sup.spawn(opening_usdc=100.0, actor="alice")
        handle = sup._darwins[status.darwin_id]
        # No rule → freelancer payment escalates.
        fac = handle.runtime.catalog["payments.facilitator"]
        nctx = handle.runtime._build_neuron_context("payments.facilitator")
        task = Task(
            id="p-baseline", kind="payment", category="content.affiliate",
            payload={
                "amount_usdc": 12.0,
                "counterparty": "upwork:writer-42",
                "counterparty_category": "freelancer",
            },
        )
        plan = await fac.plan(task, nctx)
        res = await fac.execute(plan, nctx)
        assert res.escalated is True

        # Install pre-approval, retry, now allowed.
        await sup.set_preapproval(
            status.darwin_id,
            rule_id="upwork-under-50",
            max_amount_usdc=50.0,
            counterparty_category="freelancer",
            counterparty_substring="upwork",
            actor="alice",
        )
        task2 = Task(
            id="p-allowed", kind="payment", category="content.affiliate",
            payload={
                "amount_usdc": 12.0,
                "counterparty": "upwork:writer-42",
                "counterparty_category": "freelancer",
            },
        )
        plan2 = await fac.plan(task2, nctx)
        res2 = await fac.execute(plan2, nctx)
        assert res2.ok is True
        assert res2.escalated is False
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


# ---------------------------------------------------------------------------
# Owner attribution + omnibus fund_child / sweep_child wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_captures_owner_subject_from_spawn_actor(_omnibus_chain):
    sup = Supervisor(heartbeat_seconds=0.001)
    try:
        status = await sup.spawn(opening_usdc=100.0, actor="alice")
        handle = sup._darwins[status.darwin_id]
        assert handle.owner_subject == "alice"
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_child_inherits_owner_subject_from_parent():
    """Reproduction preserves owner attribution — a parent owned by
    alice can only spawn a child owned by alice."""
    sup = Supervisor(heartbeat_seconds=0.001)
    try:
        parent_status = await sup.spawn(opening_usdc=100.0, actor="alice")
        # Use the second spawn as a synthetic child for the attribution
        # check (the genome's reproduction loop creates one too, but its
        # boot path involves DNA verification that isn't relevant here).
        child_status = await sup.spawn(
            opening_usdc=25.0, actor="alice",
            parent_id=parent_status.darwin_id,
        )
        child_handle = sup._darwins[child_status.darwin_id]
        # Direct spawns with parent_id carry the actor through; the
        # _boot_child path inherits from the parent.owner_subject so both
        # paths converge on the parent's owner.
        assert child_handle.owner_subject == "alice"
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_root_spawn_funds_via_omnibus_when_user_credit_available(
    _omnibus_chain,
):
    """Owner has deposited; spawning a root Darwin debits user_credits
    and records a FUND_CHILD entry on the omnibus ledger."""
    omnibus = OmnibusWallet(_omnibus_chain)
    omnibus.record_deposit(
        user_subject="alice", amount_usdc=200.0, source_ref="stripe_x",
    )
    sup = Supervisor(heartbeat_seconds=0.001, omnibus_wallet=omnibus)
    try:
        await sup.spawn(opening_usdc=100.0, actor="alice")
        # user_credits decremented; FUND_CHILD on ledger.
        assert omnibus.user_credit("alice") == pytest.approx(100.0)
        funds = [
            e for e in omnibus.ledger()
            if e.kind.value == "fund_child"
        ]
        assert len(funds) == 1
        assert funds[0].metadata["user_subject"] == "alice"
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_root_spawn_skips_omnibus_fund_when_anonymous(_omnibus_chain):
    """Anonymous actor has no user_credit attribution; the funding
    helper must not raise."""
    omnibus = OmnibusWallet(_omnibus_chain)
    sup = Supervisor(heartbeat_seconds=0.001, omnibus_wallet=omnibus)
    try:
        await sup.spawn(opening_usdc=100.0)  # actor defaults to anonymous
        funds = [e for e in omnibus.ledger() if e.kind.value == "fund_child"]
        assert funds == []
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_insufficient_user_credit_audit_logged_not_raised(
    _omnibus_chain,
):
    """Owner has no credit; the spawn proceeds (FakeWallet has its
    in-memory balance) and the skip is audit-logged."""
    omnibus = OmnibusWallet(_omnibus_chain)
    sup = Supervisor(heartbeat_seconds=0.001, omnibus_wallet=omnibus)
    try:
        await sup.spawn(opening_usdc=100.0, actor="bob")
        skipped = sup.audit_log.entries(action="omnibus_fund_skipped")
        assert len(skipped) == 1
        assert "insufficient" in skipped[0].metadata.get("reason", "").lower()
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_kill_credits_residual_back_to_user(_omnibus_chain):
    """A Darwin with leftover USDC at kill time credits the owning user
    via omnibus.sweep_child (recording-only — on-chain reconciliation is
    operator-driven)."""
    omnibus = OmnibusWallet(_omnibus_chain)
    omnibus.record_deposit(
        user_subject="alice", amount_usdc=200.0, source_ref="x",
    )
    sup = Supervisor(heartbeat_seconds=0.001, omnibus_wallet=omnibus)
    try:
        status = await sup.spawn(opening_usdc=100.0, actor="alice")
        # After fund_child: alice has 100 credit, Darwin has 100 wallet.
        before_kill = omnibus.user_credit("alice")
        await sup.kill(status.darwin_id, reason="test", actor="alice")
        # Residual balance (100) swept back to alice.
        after_kill = omnibus.user_credit("alice")
        assert after_kill == pytest.approx(before_kill + 100.0)
        sweeps = [e for e in omnibus.ledger() if e.kind.value == "sweep_child"]
        assert len(sweeps) == 1
    finally:
        await sup.shutdown(kill_running=False)


# ---------------------------------------------------------------------------
# Fiat-payout failure recovery (refund_fiat_payout)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refund_fiat_payout_returns_funds_and_audits(_omnibus_chain):
    """Operator-driven recovery from a failed Darwin → fiat payout:
    omnibus → Darwin wallet chain transfer + audit entry. The Darwin's
    local balance reflects the inbound via wallet.credit."""
    from darwin_platform.adapters.fiat import StubFiatRamp

    from darwin_platform.payments.freelancer_registry import (
        InMemoryFreelancerRegistry,
    )

    sup = Supervisor(
        heartbeat_seconds=0.001,
        omnibus_wallet=OmnibusWallet(_omnibus_chain),
        fiat_ramp=StubFiatRamp(),
        freelancer_registry=InMemoryFreelancerRegistry(),
    )
    try:
        status = await sup.spawn(opening_usdc=200.0, actor="alice")
        sup.register_freelancer(
            owner_subject="alice",
            freelancer_id="upwork:writer-42",
            fiat_destination="acct_x",
            actor="alice",
        )
        # Run the payout, then refund.
        await sup.darwin_fiat_payout(
            status.darwin_id,
            amount_usdc=30.0,
            freelancer_id="upwork:writer-42",
            actor="alice",
        )
        wallet = sup._darwins[status.darwin_id].stack.wallet
        balance_after_payout = wallet.balance_usdc()

        refund = await sup.refund_fiat_payout(
            status.darwin_id,
            amount_usdc=30.0,
            reason="ramp_outage",
            actor="alice",
        )
        assert refund["kind"] == "refund_to_darwin"
        # Local balance is restored (FakeWallet's credit() bumps it).
        assert wallet.balance_usdc() == pytest.approx(
            balance_after_payout + 30.0,
        )
        # Omnibus ledger has the refund entry.
        refunds = [
            e for e in sup.omnibus_wallet.ledger()
            if e.kind.value == "refund_to_darwin"
        ]
        assert len(refunds) == 1
        # Audit trail.
        audits = sup.audit_log.entries(action="refund_fiat_payout")
        assert len(audits) == 1
        assert audits[0].metadata["reason"] == "ramp_outage"
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_refund_fiat_payout_requires_omnibus():
    sup = Supervisor(heartbeat_seconds=0.001)
    try:
        status = await sup.spawn(opening_usdc=100.0, actor="alice")
        with pytest.raises(Exception, match="omnibus"):
            await sup.refund_fiat_payout(
                status.darwin_id, amount_usdc=10.0, actor="alice",
            )
    finally:
        await sup.shutdown(kill_running=True)


# ---------------------------------------------------------------------------
# Pre-approval inheritance on reproduction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_inherits_parent_preapprovals_via_helper():
    """Reproduction copies owner pre-approval rules onto the child's
    wallet. Exercises the _inherit_preapprovals helper with a synthetic
    child wallet so the test doesn't depend on the genome's DNA-verify
    path; the helper is what _boot_child actually calls."""
    sup = Supervisor(heartbeat_seconds=0.001)
    try:
        parent_status = await sup.spawn(opening_usdc=100.0, actor="alice")
        await sup.set_preapproval(
            parent_status.darwin_id,
            rule_id="upwork-under-50",
            max_amount_usdc=50.0,
            counterparty_category="freelancer",
            counterparty_substring="upwork",
            actor="alice",
        )
        child_status = await sup.spawn(
            opening_usdc=25.0, actor="alice",
            parent_id=parent_status.darwin_id,
        )
        parent = sup._darwins[parent_status.darwin_id]
        child_wallet = sup._darwins[child_status.darwin_id].stack.wallet
        # The helper is what _boot_child invokes; exercise it directly.
        sup._inherit_preapprovals(
            parent, child_wallet, child_status.darwin_id,
        )
        child_rules = child_wallet.owner_preapprovals()
        assert len(child_rules) == 1
        assert child_rules[0].rule_id == "upwork-under-50"
        # Audit captured the inheritance event.
        audits = sup.audit_log.entries(action="preapprovals_inherited")
        assert len(audits) == 1
        assert audits[0].metadata["rule_ids"] == ["upwork-under-50"]
        assert audits[0].metadata["parent_id"] == parent.darwin_id
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_inherit_preapprovals_with_no_rules_is_noop():
    sup = Supervisor(heartbeat_seconds=0.001)
    try:
        parent_status = await sup.spawn(opening_usdc=100.0, actor="alice")
        child_status = await sup.spawn(
            opening_usdc=25.0, actor="alice",
            parent_id=parent_status.darwin_id,
        )
        parent = sup._darwins[parent_status.darwin_id]
        child_wallet = sup._darwins[child_status.darwin_id].stack.wallet
        sup._inherit_preapprovals(
            parent, child_wallet, child_status.darwin_id,
        )
        assert child_wallet.owner_preapprovals() == ()
        # No audit entry when there's nothing to inherit.
        assert sup.audit_log.entries(action="preapprovals_inherited") == []
    finally:
        await sup.shutdown(kill_running=True)


# ---------------------------------------------------------------------------
# Escalation → fiat payout linkage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_darwin_fiat_payout_records_escalation_id(_omnibus_chain):
    """The operator passes the originating escalation_id; the supervisor
    threads it into both the audit entry and the omnibus ledger entry
    so a forensic timeline ties the approval to the settlement."""
    from darwin_platform.adapters.fiat import StubFiatRamp
    from darwin_platform.payments.freelancer_registry import (
        InMemoryFreelancerRegistry,
    )

    sup = Supervisor(
        heartbeat_seconds=0.001,
        omnibus_wallet=OmnibusWallet(_omnibus_chain),
        fiat_ramp=StubFiatRamp(),
        freelancer_registry=InMemoryFreelancerRegistry(),
    )
    try:
        status = await sup.spawn(opening_usdc=200.0, actor="alice")
        sup.register_freelancer(
            owner_subject="alice",
            freelancer_id="upwork:x",
            fiat_destination="acct_x",
            actor="alice",
        )
        await sup.darwin_fiat_payout(
            status.darwin_id,
            amount_usdc=10.0,
            freelancer_id="upwork:x",
            escalation_id="esc-12345",
            actor="alice",
        )
        # Audit metadata carries the link.
        audits = sup.audit_log.entries(action="darwin_fiat_payout")
        assert len(audits) == 1
        assert audits[0].metadata["escalation_id"] == "esc-12345"
        # Omnibus ledger metadata carries it too.
        entries = [
            e for e in sup.omnibus_wallet.ledger()
            if e.kind.value == "darwin_fiat_payout"
        ]
        assert len(entries) == 1
        assert entries[0].metadata["escalation_id"] == "esc-12345"
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_darwin_fiat_payout_escalation_id_optional(_omnibus_chain):
    """Calling without escalation_id is allowed but records None so the
    operator can later flag 'unattributed payouts' for review."""
    from darwin_platform.adapters.fiat import StubFiatRamp
    from darwin_platform.payments.freelancer_registry import (
        InMemoryFreelancerRegistry,
    )

    sup = Supervisor(
        heartbeat_seconds=0.001,
        omnibus_wallet=OmnibusWallet(_omnibus_chain),
        fiat_ramp=StubFiatRamp(),
        freelancer_registry=InMemoryFreelancerRegistry(),
    )
    try:
        status = await sup.spawn(opening_usdc=200.0, actor="alice")
        sup.register_freelancer(
            owner_subject="alice", freelancer_id="upwork:x",
            fiat_destination="acct_x", actor="alice",
        )
        await sup.darwin_fiat_payout(
            status.darwin_id,
            amount_usdc=10.0, freelancer_id="upwork:x", actor="alice",
        )
        entries = [
            e for e in sup.omnibus_wallet.ledger()
            if e.kind.value == "darwin_fiat_payout"
        ]
        assert entries[0].metadata["escalation_id"] is None
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_set_preapproval_threads_max_uses_to_wallet():
    """Owner-set max_uses lands on the wallet rule so the engine can
    enforce it and the facilitator's consume hook bumps used_count."""
    sup = Supervisor(heartbeat_seconds=0.001)
    try:
        status = await sup.spawn(opening_usdc=100.0, actor="alice")
        await sup.set_preapproval(
            status.darwin_id,
            rule_id="upwork-3-times",
            max_amount_usdc=20.0,
            counterparty_category="freelancer",
            counterparty_substring="upwork",
            max_uses=3,
            actor="alice",
        )
        wallet = sup._darwins[status.darwin_id].stack.wallet
        rules = wallet.owner_preapprovals()
        assert len(rules) == 1
        assert rules[0].max_uses == 3
        assert rules[0].used_count == 0
        # Audit metadata carries it too.
        audits = sup.audit_log.entries(action="set_preapproval")
        assert len(audits) == 1
        assert audits[0].metadata["max_uses"] == 3
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_preapproval_max_uses_blocks_after_quota_exhausted():
    """End-to-end via the facilitator: once max_uses spends have
    cleared, the next matching spend falls through to escalation."""
    from darwin_dna.neurons import Task

    sup = Supervisor(heartbeat_seconds=0.001)
    try:
        status = await sup.spawn(opening_usdc=200.0, actor="alice")
        await sup.set_preapproval(
            status.darwin_id,
            rule_id="r1",
            max_amount_usdc=20.0,
            counterparty_category="freelancer",
            counterparty_substring="upwork",
            max_uses=2,
            actor="alice",
        )
        handle = sup._darwins[status.darwin_id]
        fac = handle.runtime.catalog["payments.facilitator"]
        nctx = handle.runtime._build_neuron_context("payments.facilitator")

        async def _pay(task_id: str):
            task = Task(
                id=task_id, kind="payment", category="content.affiliate",
                payload={
                    "amount_usdc": 5.0,
                    "counterparty": "upwork:writer-1",
                    "counterparty_category": "freelancer",
                },
            )
            plan = await fac.plan(task, nctx)
            return await fac.execute(plan, nctx)

        # First two spends: cleared by pre-approval.
        r1 = await _pay("p1")
        assert r1.ok and not r1.escalated
        r2 = await _pay("p2")
        assert r2.ok and not r2.escalated
        # Third spend: quota exhausted → escalation.
        r3 = await _pay("p3")
        assert r3.escalated is True
    finally:
        await sup.shutdown(kill_running=True)
