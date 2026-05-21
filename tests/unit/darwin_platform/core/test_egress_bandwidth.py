"""Tests for the Phase 2 egress bandwidth quota (docs/engineering/22).

Coverage:
- BandwidthQuota: fresh-day rollover, soft + hard cap behaviour.
- check_and_charge increments usage; refuses above hard cap.
- Anonymous traffic (no darwin_id) is exempt but bucketed.
- soft_breach_first_time fires once per Darwin per day.
- snapshot exposes per-Darwin usage.
"""

from __future__ import annotations

import pytest

from darwin_platform.egress_proxy import BandwidthQuota


def test_initial_usage_zero():
    q = BandwidthQuota()
    assert q.used_today("d-1") == 0
    assert q.remaining_today("d-1") == q.hard_bytes_per_day


def test_check_and_charge_increments_usage():
    q = BandwidthQuota(soft_bytes_per_day=1_000, hard_bytes_per_day=2_000)
    ok, reason, soft_first = q.check_and_charge(
        darwin_id="d-1", bytes_used=300,
    )
    assert ok and reason is None and soft_first is False
    assert q.used_today("d-1") == 300


def test_soft_breach_fires_once():
    q = BandwidthQuota(soft_bytes_per_day=500, hard_bytes_per_day=2_000)
    # Below soft.
    _, _, sf1 = q.check_and_charge(darwin_id="d-1", bytes_used=300)
    # Above soft, first time.
    _, _, sf2 = q.check_and_charge(darwin_id="d-1", bytes_used=300)
    # Above soft, repeat.
    _, _, sf3 = q.check_and_charge(darwin_id="d-1", bytes_used=100)
    assert sf1 is False
    assert sf2 is True   # first breach
    assert sf3 is False  # already warned


def test_hard_cap_refuses_overage():
    q = BandwidthQuota(soft_bytes_per_day=500, hard_bytes_per_day=1_000)
    q.check_and_charge(darwin_id="d-1", bytes_used=900)
    ok, reason, _ = q.check_and_charge(darwin_id="d-1", bytes_used=200)
    assert ok is False
    assert "cap" in reason


def test_anonymous_traffic_bypasses_hard_cap_but_records():
    q = BandwidthQuota(soft_bytes_per_day=100, hard_bytes_per_day=200)
    for _ in range(5):
        ok, reason, _ = q.check_and_charge(
            darwin_id=None, bytes_used=100,
        )
        assert ok is True  # anonymous traffic always allowed
    snap = q.snapshot()
    assert snap["per_darwin_usage"]["__anonymous__"] == 500


def test_snapshot_shape():
    q = BandwidthQuota()
    q.check_and_charge(darwin_id="d-1", bytes_used=50)
    snap = q.snapshot()
    assert "day" in snap
    assert snap["soft_bytes_per_day"] == q.soft_bytes_per_day
    assert snap["hard_bytes_per_day"] == q.hard_bytes_per_day
    assert snap["per_darwin_usage"]["d-1"] == 50
