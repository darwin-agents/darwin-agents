"""Tests for the recurring harvest scheduler (docs/engineering/05).

Covers HarvestScheduler primitives + supervisor wiring + HTTP routes.
"""

from __future__ import annotations

import pytest

from darwin_platform.harvest_scheduler import (
    HarvestRule,
    HarvestRuleError,
    HarvestScheduler,
)


# ---------------------------------------------------------------------------
# Scheduler primitives
# ---------------------------------------------------------------------------

def test_add_rule_returns_record():
    now = [1000.0]
    s = HarvestScheduler(clock=lambda: now[0])
    rule = s.add(
        darwin_id="d-1", destination="0xowner",
        trigger_balance_usdc=100.0, amount_usdc=50.0, interval_seconds=3600,
    )
    assert rule.darwin_id == "d-1"
    assert rule.enabled is True
    assert rule.last_run_at is None


def test_add_rule_validation():
    s = HarvestScheduler()
    with pytest.raises(HarvestRuleError):
        s.add(darwin_id="d-1", destination="", trigger_balance_usdc=100)
    with pytest.raises(HarvestRuleError):
        s.add(darwin_id="d-1", destination="x", trigger_balance_usdc=-1)
    with pytest.raises(HarvestRuleError):
        s.add(darwin_id="d-1", destination="x", trigger_balance_usdc=100,
              amount_usdc=-1)
    with pytest.raises(HarvestRuleError):
        s.add(darwin_id="d-1", destination="x", trigger_balance_usdc=100,
              interval_seconds=5)  # below the 60s floor


def test_due_returns_empty_below_trigger():
    s = HarvestScheduler()
    s.add(darwin_id="d-1", destination="x", trigger_balance_usdc=100.0)
    assert s.due(darwin_id="d-1", balance_usdc=50.0) == []


def test_due_respects_interval():
    now = [1000.0]
    s = HarvestScheduler(clock=lambda: now[0])
    rule = s.add(
        darwin_id="d-1", destination="x",
        trigger_balance_usdc=100, amount_usdc=20, interval_seconds=3600,
    )
    # Trigger fires once.
    due = s.due(darwin_id="d-1", balance_usdc=200.0)
    assert len(due) == 1
    s.mark_run(rule.id, amount=20)
    # Immediately again — too soon.
    now[0] = 1100.0  # only 100s later
    assert s.due(darwin_id="d-1", balance_usdc=200.0) == []
    # Past the interval — fires again.
    now[0] = 5000.0
    assert len(s.due(darwin_id="d-1", balance_usdc=200.0)) == 1


def test_due_amount_for_all_above_trigger():
    s = HarvestScheduler()
    s.add(
        darwin_id="d-1", destination="x",
        trigger_balance_usdc=100, amount_usdc=None,
    )
    due = s.due(darwin_id="d-1", balance_usdc=250.0)
    assert len(due) == 1
    # "Everything above the trigger" = 250 - 100 = 150
    assert due[0].amount_usdc == pytest.approx(150.0)


def test_due_amount_fixed_respects_floor():
    s = HarvestScheduler()
    s.add(
        darwin_id="d-1", destination="x",
        trigger_balance_usdc=100, amount_usdc=50,
    )
    # Balance 130 — harvest 50 would leave 80, below trigger 100. Refuse.
    assert s.due(darwin_id="d-1", balance_usdc=130.0) == []
    # Balance 160 — harvest 50 leaves 110, above trigger. OK.
    assert len(s.due(darwin_id="d-1", balance_usdc=160.0)) == 1


def test_disable_pauses_rule():
    s = HarvestScheduler()
    rule = s.add(
        darwin_id="d-1", destination="x", trigger_balance_usdc=100,
    )
    s.set_enabled(rule.id, False)
    assert s.due(darwin_id="d-1", balance_usdc=500.0) == []
    s.set_enabled(rule.id, True)
    assert len(s.due(darwin_id="d-1", balance_usdc=500.0)) == 1


def test_remove_rule():
    s = HarvestScheduler()
    rule = s.add(
        darwin_id="d-1", destination="x", trigger_balance_usdc=100,
    )
    s.remove(rule.id)
    assert s.list("d-1") == []
    with pytest.raises(HarvestRuleError):
        s.remove(rule.id)


# ---------------------------------------------------------------------------
# Supervisor wiring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_supervisor_add_and_list_rule():
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=100.0)
    rule = sup.add_harvest_rule(
        status.darwin_id,
        destination="0xowner",
        trigger_balance_usdc=50.0,
        amount_usdc=20.0,
        interval_seconds=60,
        actor="alice",
    )
    rules = sup.list_harvest_rules(status.darwin_id)
    assert len(rules) == 1
    assert rules[0].id == rule.id
    # Audit chain has the rule add.
    entries = sup._audit.entries(action="add_harvest_rule")
    assert len(entries) == 1
    assert entries[0].actor == "alice"
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_remove_rule_audits():
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=100.0)
    rule = sup.add_harvest_rule(
        status.darwin_id, destination="0xowner",
        trigger_balance_usdc=50.0,
    )
    sup.remove_harvest_rule(status.darwin_id, rule.id, actor="alice")
    entries = sup._audit.entries(action="remove_harvest_rule")
    assert len(entries) == 1
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_drive_cycle_fires_rule():
    """A rule whose interval and trigger are met fires during the drive
    cycle and produces a harvest_rule_fired audit entry."""
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor(heartbeat_seconds=0)
    # Make every cycle evaluate rules.
    sup._harvest_eval_every_n = 1
    status = await sup.spawn(opening_usdc=1000.0)
    sup.add_harvest_rule(
        status.darwin_id, destination="0xowner",
        trigger_balance_usdc=100.0,
        amount_usdc=50.0, interval_seconds=60,
    )
    # Let a few drive cycles run.
    import asyncio
    await asyncio.sleep(0.2)
    fired = sup._audit.entries(action="harvest_rule_fired")
    assert len(fired) >= 1
    await sup.shutdown(kill_running=True)


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http_create_list_delete():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=200.0)
    did = status.darwin_id
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.post(
            f"/darwins/{did}/harvest_rules",
            json={
                "destination": "0xowner",
                "trigger_balance_usdc": 50.0,
                "amount_usdc": 20.0,
                "interval_seconds": 3600,
            },
        )
        assert r.status_code == 201
        rule_id = r.json()["id"]

        r2 = client.get(f"/darwins/{did}/harvest_rules")
        assert r2.status_code == 200
        assert len(r2.json()["rules"]) == 1

        r3 = client.delete(f"/darwins/{did}/harvest_rules/{rule_id}")
        assert r3.status_code == 200

        r4 = client.get(f"/darwins/{did}/harvest_rules")
        assert r4.json()["rules"] == []

    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_http_422_on_bad_trigger():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=200.0)
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.post(
            f"/darwins/{status.darwin_id}/harvest_rules",
            json={"destination": "0x", "trigger_balance_usdc": -1},
        )
        assert r.status_code == 422
    await sup.shutdown(kill_running=True)
