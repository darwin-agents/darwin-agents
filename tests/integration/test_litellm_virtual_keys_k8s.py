"""Per-Darwin LiteLLM virtual keys in K8s mode (doc 13).

In k8s mode the supervisor mints each Darwin's virtual key BEFORE the pod
boots and injects it via the per-Darwin Secret (so the pod authenticates as
its own key, never the shared fleet master key), revokes it on death, and
reconciles the proxy's per-key spend against the pod-reported ledger spend.
These tests use an in-memory fake driver — no real cluster.
"""

from __future__ import annotations

import pytest

from darwin_platform.adapters import LiteLLMAdminClient, LiteLLMSpendReconciler
from darwin_platform.k8s.manifests import LITELLM_VIRTUAL_KEY_SECRET_KEY
from darwin_platform.runtime.driver import DarwinSnapshot, DarwinSpec
from darwin_platform.runtime.supervisor import Supervisor


class _StubMinter:
    def __init__(self):
        self.generated: list[dict] = []
        self.deleted: list[str] = []
        self._n = 0

    def generate_key(self, *, darwin_id, **opts):
        self._n += 1
        key = f"sk-darwin-{self._n}"
        self.generated.append({"darwin_id": darwin_id, "opts": opts, "key": key})
        return {"key": key}

    def delete_key(self, key):
        self.deleted.append(key)


class _FakeK8sDriver:
    def __init__(
        self,
        *,
        llm_spend_usdc: float = 0.0,
        fail_start: bool = False,
        snapshot_balance: float = 99.0,
        merkle_root: str | None = None,
    ):
        self.started: list[DarwinSpec] = []
        self.killed: list[tuple[str, str]] = []
        self._llm_spend = llm_spend_usdc
        self._fail_start = fail_start
        self._snapshot_balance = snapshot_balance
        self._merkle_root = merkle_root

    async def start(self, spec: DarwinSpec) -> DarwinSnapshot:
        self.started.append(spec)
        if self._fail_start:
            raise RuntimeError("pod never became ready")
        return DarwinSnapshot(
            darwin_id=spec.darwin_id, alive=True, paused=False, killed=False,
            tick=0, days_elapsed=0.0, balance_usdc=spec.opening_usdc,
        )

    async def stop(self, darwin_id): ...
    async def pause(self, darwin_id, *, reason): ...
    async def resume(self, darwin_id): ...
    async def kill(self, darwin_id, *, reason):
        self.killed.append((darwin_id, reason))

    async def force_reproduce(self, darwin_id, **_): ...

    async def snapshot(self, darwin_id) -> DarwinSnapshot:
        return DarwinSnapshot(
            darwin_id=darwin_id, alive=True, paused=False, killed=False,
            tick=1, days_elapsed=0.1, balance_usdc=self._snapshot_balance,
            llm_spend_usdc=self._llm_spend, merkle_root=self._merkle_root,
        )

    async def events_records(self, darwin_id): return []
    async def list_ids(self): return []
    async def reconcile(self): return []


class _CaptureAlerts:
    def __init__(self):
        self.alerts: list = []

    def emit(self, alert):
        self.alerts.append(alert)


class _SpendResp:
    def __init__(self, spend):
        self.status_code = 200
        self.text = ""
        self._spend = spend

    def json(self):
        return {"info": {"spend": self._spend}}


class _SpendHttp:
    def __init__(self, spend):
        self._spend = spend

    def get(self, url, **_):
        return _SpendResp(self._spend)

    def post(self, url, **_):
        return _SpendResp(self._spend)


# -- spawn-time minting + injection ---------------------------------------
async def test_spawn_k8s_mints_and_injects_virtual_key():
    minter = _StubMinter()
    driver = _FakeK8sDriver()
    sup = Supervisor(
        k8s_driver=driver,
        heartbeat_seconds=0.0,
        llm_key_minter=minter,
        llm_virtual_key_options={"max_budget": 40.0},
    )
    try:
        status = await sup.spawn(opening_usdc=100.0)
        did = status.darwin_id
        assert len(minter.generated) == 1
        assert minter.generated[0]["darwin_id"] == did
        assert minter.generated[0]["opts"] == {"max_budget": 40.0}
        # The key rides into the pod via the per-Darwin Secret, not as the
        # shared master key.
        spec = driver.started[0]
        assert spec.secret_extras[LITELLM_VIRTUAL_KEY_SECRET_KEY] == "sk-darwin-1"
        assert sup._llm_virtual_keys[did] == "sk-darwin-1"  # noqa: SLF001
    finally:
        await sup.shutdown()


async def test_spawn_k8s_without_minter_injects_no_key():
    driver = _FakeK8sDriver()
    sup = Supervisor(k8s_driver=driver, heartbeat_seconds=0.0)
    try:
        await sup.spawn(opening_usdc=100.0)
        assert driver.started[0].secret_extras == {}
    finally:
        await sup.shutdown()


async def test_spawn_k8s_mint_failure_aborts_before_pod_start():
    class _Boom:
        def generate_key(self, *, darwin_id, **opts):
            raise RuntimeError("proxy 500")

        def delete_key(self, key): ...

    driver = _FakeK8sDriver()
    sup = Supervisor(
        k8s_driver=driver, heartbeat_seconds=0.0, llm_key_minter=_Boom(),
    )
    try:
        with pytest.raises(RuntimeError, match="proxy 500"):
            await sup.spawn(opening_usdc=100.0)
        # The pod must never be created if we couldn't mint its key.
        assert driver.started == []
    finally:
        await sup.shutdown()


async def test_spawn_k8s_pod_start_failure_revokes_minted_key():
    minter = _StubMinter()
    driver = _FakeK8sDriver(fail_start=True)
    sup = Supervisor(
        k8s_driver=driver, heartbeat_seconds=0.0, llm_key_minter=minter,
    )
    try:
        with pytest.raises(Exception):
            await sup.spawn(opening_usdc=100.0)
        # The orphaned key is revoked so it can't linger on the proxy.
        assert minter.deleted == ["sk-darwin-1"]
        assert sup._llm_virtual_keys == {}  # noqa: SLF001
    finally:
        await sup.shutdown()


async def test_kill_k8s_revokes_virtual_key():
    minter = _StubMinter()
    driver = _FakeK8sDriver()
    sup = Supervisor(
        k8s_driver=driver, heartbeat_seconds=0.0, llm_key_minter=minter,
    )
    status = await sup.spawn(opening_usdc=100.0)
    try:
        await sup.kill(status.darwin_id, actor="owner")
        assert minter.deleted == ["sk-darwin-1"]
    finally:
        await sup.shutdown()


# -- reconciliation --------------------------------------------------------
async def test_k8s_reconcile_flags_drift():
    minter = _StubMinter()
    # Pod reports 2.0 of LLM ledger spend; proxy metered 1.0 → expected 1.10
    # at 10% markup → drift 0.90, beyond tolerance.
    driver = _FakeK8sDriver(llm_spend_usdc=2.0)
    admin = LiteLLMAdminClient(
        base_url="http://litellm:4000", master_key="m", http=_SpendHttp(1.0),
    )
    reconciler = LiteLLMSpendReconciler(
        admin_client=admin, markup_rate=0.10, tolerance_usdc=0.05,
    )
    alerts = _CaptureAlerts()
    sup = Supervisor(
        k8s_driver=driver,
        heartbeat_seconds=0.0,
        alert_sink=alerts,
        llm_key_minter=minter,
        llm_spend_reconciler=reconciler,
    )
    status = await sup.spawn(opening_usdc=100.0)
    did = status.darwin_id
    try:
        await sup._areconcile_llm_spend_k8s()  # noqa: SLF001
        drift = [a for a in alerts.alerts if a.kind == "llm_spend_drift"]
        assert len(drift) == 1
        assert drift[0].detail["darwin_id"] == did
        assert drift[0].detail["drift_usdc"] == pytest.approx(0.90)
    finally:
        await sup.shutdown()


async def test_k8s_reconcile_within_tolerance_no_alert():
    minter = _StubMinter()
    driver = _FakeK8sDriver(llm_spend_usdc=1.10)  # == expected (1.0 * 1.10)
    admin = LiteLLMAdminClient(
        base_url="http://litellm:4000", master_key="m", http=_SpendHttp(1.0),
    )
    reconciler = LiteLLMSpendReconciler(
        admin_client=admin, markup_rate=0.10, tolerance_usdc=0.05,
    )
    alerts = _CaptureAlerts()
    sup = Supervisor(
        k8s_driver=driver,
        heartbeat_seconds=0.0,
        alert_sink=alerts,
        llm_key_minter=minter,
        llm_spend_reconciler=reconciler,
    )
    await sup.spawn(opening_usdc=100.0)
    try:
        await sup._areconcile_llm_spend_k8s()  # noqa: SLF001
        assert [a for a in alerts.alerts if a.kind == "llm_spend_drift"] == []
    finally:
        await sup.shutdown()


# -- Merkle anchoring + omnibus reconciliation in k8s ----------------------
async def test_k8s_fleet_maintenance_publishes_merkle_anchor():
    """The daily fleet Merkle anchor (doc 29) is built from each pod's audit
    root, collected via snapshot — it never ran in k8s mode before."""
    from darwin_platform.observability.merkle_anchor import (
        MerkleAnchorPublisher,
        StubAnchorBackend,
    )

    backend = StubAnchorBackend()
    driver = _FakeK8sDriver(merkle_root="rootabc")
    sup = Supervisor(
        k8s_driver=driver,
        heartbeat_seconds=0.0,
        merkle_anchor_publisher=MerkleAnchorPublisher(backend=backend),
    )
    status = await sup.spawn(opening_usdc=100.0)
    try:
        await sup._run_k8s_fleet_maintenance(now=0.0)  # noqa: SLF001
        # The anchor was published (one fleet root over the pod's root) and
        # audited.
        anchored = sup.audit_log.entries(action="merkle_anchor")
        assert len(anchored) == 1
        assert anchored[0].metadata["darwin_count"] == 1
        assert status.darwin_id  # spawned via driver
    finally:
        await sup.shutdown()


async def test_k8s_fleet_maintenance_flags_omnibus_discrepancy():
    """Omnibus reconciliation (doc 05) sums each pod's on-chain balance from
    its snapshot; a mismatch against the omnibus pages on-call."""
    from darwin_platform.payments.omnibus_wallet import OmnibusWallet
    from tests.unit.darwin_platform.payments.test_omnibus_wallet import _Chain

    # Fresh omnibus holding 0 on-chain; a pod reporting a 99 USDC balance
    # makes the books disagree (discrepancy ≈ 99 ≫ the 1¢ tolerance).
    omnibus = OmnibusWallet(_Chain(balance=0.0))
    driver = _FakeK8sDriver(snapshot_balance=99.0)
    alerts = _CaptureAlerts()
    sup = Supervisor(
        k8s_driver=driver,
        heartbeat_seconds=0.0,
        alert_sink=alerts,
        omnibus_wallet=omnibus,
    )
    await sup.spawn(opening_usdc=100.0)
    try:
        await sup._run_k8s_fleet_maintenance(now=0.0)  # noqa: SLF001
        disc = [
            a for a in alerts.alerts
            if a.kind == "omnibus_reconcile_discrepancy"
        ]
        assert len(disc) == 1
    finally:
        await sup.shutdown()


async def test_k8s_fleet_maintenance_omnibus_balanced_no_alert():
    from darwin_platform.payments.omnibus_wallet import OmnibusWallet
    from tests.unit.darwin_platform.payments.test_omnibus_wallet import _Chain

    # Pods report 0 balance → sum matches the empty omnibus → no discrepancy.
    omnibus = OmnibusWallet(_Chain(balance=0.0))
    driver = _FakeK8sDriver(snapshot_balance=0.0)
    alerts = _CaptureAlerts()
    sup = Supervisor(
        k8s_driver=driver,
        heartbeat_seconds=0.0,
        alert_sink=alerts,
        omnibus_wallet=omnibus,
    )
    await sup.spawn(opening_usdc=100.0)
    try:
        await sup._run_k8s_fleet_maintenance(now=0.0)  # noqa: SLF001
        assert [
            a for a in alerts.alerts
            if a.kind == "omnibus_reconcile_discrepancy"
        ] == []
    finally:
        await sup.shutdown()
