"""Fleet-wide incident-response levers (doc 09).

The platform previously had only per-Darwin owner controls; there was no
fleet-wide pause / spend-lock / category-block / counterparty-block — the
levers an operator reaches for during an incident. These pin them.
"""

from __future__ import annotations

import pytest

from darwin_platform.payments.sanctions import LocalDenylistScreener
from darwin_platform.runtime.supervisor import Supervisor, SupervisorError


async def _spawn_n(sup, n):
    ids = []
    for _ in range(n):
        s = await sup.spawn(opening_usdc=100.0, actor="op")
        ids.append(s.darwin_id)
    return ids


@pytest.mark.asyncio
async def test_fleet_pause_and_resume():
    sup = Supervisor(heartbeat_seconds=0.001)
    ids = await _spawn_n(sup, 3)
    try:
        out = await sup.fleet_pause(reason="incident", actor="op")
        assert set(out["paused"]) == set(ids)
        out = await sup.fleet_resume(actor="op")
        assert set(out["resumed"]) >= set(ids)
        assert sup.audit_log.entries(action="fleet_pause")
        assert sup.audit_log.entries(action="fleet_resume")
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_fleet_spend_lock_freezes_all_wallets():
    sup = Supervisor(heartbeat_seconds=0.001)
    ids = await _spawn_n(sup, 2)
    try:
        out = await sup.fleet_spend_lock(locked=True, actor="op")
        assert set(out["spend_locked"]) == set(ids)
        for did in ids:
            wallet = sup._darwins[did].stack.wallet
            assert wallet.spend_lock()["spend_locked"] is True
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_fleet_block_category_is_additive_across_fleet():
    sup = Supervisor(heartbeat_seconds=0.001)
    ids = await _spawn_n(sup, 2)
    try:
        out = await sup.fleet_block_category(category="trading.spot", actor="op")
        assert set(out["blocked_on"]) == set(ids)
        for did in ids:
            assert "trading.spot" in sup._darwins[did].disabled_categories
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_block_counterparty_adds_to_local_screener():
    screener = LocalDenylistScreener()
    sup = Supervisor(heartbeat_seconds=0.001, sanctions_screener=screener)
    try:
        bad = "0x" + "ab" * 20
        assert screener.check(bad).allowed is True
        sup.block_counterparty(bad, actor="op")
        assert screener.check(bad).allowed is False
        assert sup.audit_log.entries(action="block_counterparty")
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_block_counterparty_refuses_without_local_screener():
    sup = Supervisor(heartbeat_seconds=0.001)  # no screener
    try:
        with pytest.raises(SupervisorError, match="does not support"):
            sup.block_counterparty("0x" + "cd" * 20, actor="op")
    finally:
        await sup.shutdown(kill_running=True)
