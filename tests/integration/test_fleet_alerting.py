"""Fleet-maintenance CRITICAL alerting (docs 05/29) + open-intent reconciliation.

Omnibus reconciliation discrepancies, Merkle anchor failures, and dangling
payment intents are P0 — they must page on-call (CRITICAL alert → PagerDuty),
not merely land in the audit log where a human has to notice them.
"""

from __future__ import annotations

import asyncio

import pytest

from darwin_platform.observability.monitoring import Severity
from darwin_platform.payments.omnibus_wallet import OmnibusWallet
from darwin_platform.runtime.supervisor import Supervisor


class _RecordingSink:
    def __init__(self):
        self.alerts = []

    def emit(self, alert) -> None:
        self.alerts.append(alert)


def test_omnibus_default_tolerance_is_one_cent():
    from tests.unit.darwin_platform.payments.test_omnibus_wallet import _Chain

    w = OmnibusWallet(_Chain(balance=0.0))
    assert w._tolerance == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_fleet_alert_emits_critical_through_sink():
    sink = _RecordingSink()
    sup = Supervisor(heartbeat_seconds=0.001, alert_sink=sink)
    try:
        sup._fleet_alert("omnibus_reconcile_discrepancy", {"discrepancy_usdc": 2.5})
        assert len(sink.alerts) == 1
        a = sink.alerts[0]
        assert a.severity is Severity.CRITICAL
        assert a.kind == "omnibus_reconcile_discrepancy"
        assert a.suggested_level == 5
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_open_intent_pages_once_after_restart():
    sink = _RecordingSink()
    sup = Supervisor(
        heartbeat_seconds=0.001,
        maintenance_interval_seconds=0.05,
        alert_sink=sink,
    )
    status = await sup.spawn(opening_usdc=100.0, actor="test")
    did = status.darwin_id
    # Simulate a crash between an on-chain transfer and its log_outcome.
    sup._darwins[did].runtime.audit.log_intent(
        "payment.intent", {"amount_usdc": 5.0, "to": "0xabc"},
    )
    try:
        await asyncio.sleep(0.4)  # several maintenance cycles
        open_alerts = [
            a for a in sink.alerts if a.kind == "open_intents_unreconciled"
        ]
        # Paged at least once...
        assert len(open_alerts) >= 1
        # ...but not on every single cycle (deduped per darwin).
        assert len(open_alerts) <= 2
        assert open_alerts[0].detail["darwin_id"] == did
        # And it landed in the operator audit log.
        assert sup.audit_log.entries(action="open_intents_unreconciled")
    finally:
        await sup.shutdown(kill_running=True)
