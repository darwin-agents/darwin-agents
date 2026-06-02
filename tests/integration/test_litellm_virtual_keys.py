"""Per-Darwin LiteLLM virtual keys, end-to-end through the Supervisor (doc 13).

Covers the wiring the docs-vs-impl audit flagged as missing: a virtual key
minted per Darwin at spawn (not a shared master key), threaded into the
transport, sealed in the vault, revoked on death, and a spend reconciler that
pages on proxy-vs-ledger drift.
"""

from __future__ import annotations

import pytest

from darwin_platform import BillingPolicy, Supervisor
from darwin_platform.adapters import (
    LiteLLMAdminClient,
    LiteLLMSpendReconciler,
    LiteLLMTransport,
    TierMapping,
)


# -- stubs -----------------------------------------------------------------
class _StubMinter:
    """Stand-in for LiteLLMAdminClient: hands out incrementing keys, records
    every generate/delete so the test can assert per-Darwin minting + revoke."""

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


class _CaptureAlerts:
    def __init__(self):
        self.alerts: list = []

    def emit(self, alert):
        self.alerts.append(alert)


def _llm_factory_recording(record):
    def factory(wallet, clock, *, darwin_id=None, virtual_key=None):
        record["darwin_id"] = darwin_id
        record["virtual_key"] = virtual_key
        record["wallet"] = wallet
        # A minimal real transport so boot() gets a valid LLMTransport.
        return LiteLLMTransport(
            wallet=wallet,
            tier_mappings={"reasoning": TierMapping(models=("m",))},
            litellm_completion=lambda **_: type(
                "R", (), {"choices": [], "usage": None}
            )(),
            litellm_completion_cost=lambda **_: 0.0,
            darwin_id=darwin_id,
        )

    return factory


# -- spawn-time minting ----------------------------------------------------
async def test_spawn_mints_per_darwin_key_threaded_into_transport():
    minter = _StubMinter()
    record: dict = {}
    sup = Supervisor(
        heartbeat_seconds=0.0,
        llm_transport_factory=_llm_factory_recording(record),
        llm_key_minter=minter,
        llm_virtual_key_options={"max_budget": 25.0},
    )
    try:
        status = await sup.spawn(opening_usdc=100.0, actor="owner")
        did = status.darwin_id
        # A fresh key was minted scoped to this darwin_id (with the budget).
        assert len(minter.generated) == 1
        assert minter.generated[0]["darwin_id"] == did
        assert minter.generated[0]["opts"] == {"max_budget": 25.0}
        # ...and threaded into the transport (not the shared master key).
        assert record["virtual_key"] == "sk-darwin-1"
        assert record["darwin_id"] == did
        # ...and sealed into the Darwin's vault for restart recovery.
        h = sup._darwins[did]  # noqa: SLF001
        assert h.stack.vault.get("litellm/virtual_key") == "sk-darwin-1"
    finally:
        await sup.shutdown()


async def test_kill_revokes_virtual_key():
    minter = _StubMinter()
    sup = Supervisor(
        heartbeat_seconds=0.0,
        llm_transport_factory=_llm_factory_recording({}),
        llm_key_minter=minter,
    )
    status = await sup.spawn(opening_usdc=100.0, actor="owner")
    did = status.darwin_id
    try:
        await sup.kill(did, actor="owner")
        assert minter.deleted == ["sk-darwin-1"]
    finally:
        await sup.shutdown()


async def test_mint_failure_aborts_spawn():
    class _Boom:
        def generate_key(self, *, darwin_id, **opts):
            raise RuntimeError("proxy 500")

        def delete_key(self, key):
            pass

    sup = Supervisor(
        heartbeat_seconds=0.0,
        llm_transport_factory=_llm_factory_recording({}),
        llm_key_minter=_Boom(),
    )
    try:
        with pytest.raises(RuntimeError, match="proxy 500"):
            await sup.spawn(opening_usdc=100.0, actor="owner")
    finally:
        await sup.shutdown()


async def test_no_minter_leaves_virtual_key_none():
    record: dict = {}
    sup = Supervisor(
        heartbeat_seconds=0.0,
        llm_transport_factory=_llm_factory_recording(record),
    )
    try:
        await sup.spawn(opening_usdc=100.0, actor="owner")
        # No minter → falls back to the shared master key (virtual_key None).
        assert record["virtual_key"] is None
    finally:
        await sup.shutdown()


# -- spend reconciliation --------------------------------------------------
class _SpendResp:
    def __init__(self, spend):
        self.status_code = 200
        self.text = ""
        self._spend = spend

    def json(self):
        return {"info": {"spend": self._spend}}


class _SpendHttp:
    """Minimal httpx stub returning a fixed proxy spend for /key/info."""

    def __init__(self, spend):
        self._spend = spend

    def get(self, url, **_):
        return _SpendResp(self._spend)

    def post(self, url, **_):
        return _SpendResp(self._spend)


async def test_reconcile_flags_drift_and_alerts():
    minter = _StubMinter()
    # Proxy reports provider cost 1.0 → expected ledger 1.10 at 10% markup.
    admin = LiteLLMAdminClient(
        base_url="http://litellm:4000", master_key="m",
        http=_SpendHttp(spend=1.0),
    )
    reconciler = LiteLLMSpendReconciler(
        admin_client=admin, markup_rate=0.10, tolerance_usdc=0.05,
    )
    alerts = _CaptureAlerts()
    sup = Supervisor(
        heartbeat_seconds=0.0,
        alert_sink=alerts,
        billing_policy=BillingPolicy(infrastructure_markup_rate=0.10),
        llm_transport_factory=_llm_factory_recording({}),
        llm_key_minter=minter,
        llm_spend_reconciler=reconciler,
    )
    status = await sup.spawn(opening_usdc=100.0, actor="owner")
    did = status.darwin_id
    try:
        # Ledger shows 2.00 of LLM debits but proxy only metered 1.0
        # (expected 1.10) → 0.90 drift, well beyond tolerance.
        sup._darwins[did].stack.wallet.debit(2.00, "llm:reasoning")  # noqa: SLF001
        sup._reconcile_llm_spend(now=0.0)  # noqa: SLF001
        drift_alerts = [a for a in alerts.alerts if a.kind == "llm_spend_drift"]
        assert len(drift_alerts) == 1
        assert drift_alerts[0].detail["darwin_id"] == did
        assert drift_alerts[0].detail["drift_usdc"] == pytest.approx(0.90)
    finally:
        await sup.shutdown()


async def test_reconcile_within_tolerance_no_alert():
    minter = _StubMinter()
    admin = LiteLLMAdminClient(
        base_url="http://litellm:4000", master_key="m",
        http=_SpendHttp(spend=1.0),
    )
    reconciler = LiteLLMSpendReconciler(
        admin_client=admin, markup_rate=0.10, tolerance_usdc=0.05,
    )
    alerts = _CaptureAlerts()
    sup = Supervisor(
        heartbeat_seconds=0.0,
        alert_sink=alerts,
        llm_transport_factory=_llm_factory_recording({}),
        llm_key_minter=minter,
        llm_spend_reconciler=reconciler,
    )
    status = await sup.spawn(opening_usdc=100.0, actor="owner")
    did = status.darwin_id
    try:
        # Ledger 1.10 == expected (1.0 * 1.10) → within tolerance.
        sup._darwins[did].stack.wallet.debit(1.10, "llm:reasoning")  # noqa: SLF001
        sup._reconcile_llm_spend(now=0.0)  # noqa: SLF001
        assert [a for a in alerts.alerts if a.kind == "llm_spend_drift"] == []
    finally:
        await sup.shutdown()
