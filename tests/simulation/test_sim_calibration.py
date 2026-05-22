"""Tests for sim calibration hooks (doc 18)."""

from __future__ import annotations

import json

import pytest

from tools.sim.calibration import (
    CalibrationDataset,
    apply_calibration,
    extract_from_billing,
)
from tools.sim.world import RevenueModel, SimWorld


def test_dataset_latest_picks_newest_per_metric():
    ds = CalibrationDataset()
    ds.add(metric="compute_usd_per_day", value=0.05, source="audit",
           observed_at=100.0)
    ds.add(metric="compute_usd_per_day", value=0.08, source="audit",
           observed_at=200.0)  # newer
    point = ds.latest("compute_usd_per_day")
    assert point.value == 0.08


def test_dataset_serialises_roundtrip(tmp_path):
    ds = CalibrationDataset()
    ds.add(metric="compute_usd_per_day", value=0.04, source="audit",
           observed_at=100.0)
    blob = json.dumps(ds.to_json())
    ds2 = CalibrationDataset.from_json(json.loads(blob))
    assert len(ds2.points) == 1
    assert ds2.points[0].value == 0.04


def test_apply_calibration_overrides_compute_rate():
    rm = RevenueModel()
    sim = SimWorld(start_ts=0.0, revenue=rm, compute_usd_per_day=0.02)
    pre_rate = sim._compute_rate  # noqa: SLF001
    ds = CalibrationDataset()
    ds.add(metric="compute_usd_per_day", value=0.20,
           source="audit:fleet")
    summary = apply_calibration(
        sim_world=sim, revenue_model=rm, dataset=ds,
    )
    assert "compute_usd_per_day" in summary
    # 10x rate change reflected.
    assert sim._compute_rate == pytest.approx(0.20 / 86_400.0)  # noqa: SLF001
    assert sim._compute_rate > pre_rate  # noqa: SLF001


def test_apply_calibration_overrides_revenue_knobs():
    rm = RevenueModel(usd_per_effective_piece=1.6, quality=0.72)
    sim = SimWorld(start_ts=0.0, revenue=rm)
    ds = CalibrationDataset()
    ds.add(metric="revenue_usd_per_effective_piece", value=2.4,
           source="audit")
    ds.add(metric="revenue_quality", value=0.85, source="audit")
    apply_calibration(sim_world=sim, revenue_model=rm, dataset=ds)
    assert rm.usd_per_effective_piece == 2.4
    assert rm.quality == 0.85


def test_apply_calibration_ignores_unknown_metrics():
    rm = RevenueModel()
    sim = SimWorld(start_ts=0.0, revenue=rm)
    ds = CalibrationDataset()
    ds.add(metric="unknown_thing", value=999.0, source="x")
    summary = apply_calibration(
        sim_world=sim, revenue_model=rm, dataset=ds,
    )
    # No known metric → empty summary, no change to the sim.
    assert summary == {}


def test_extract_from_billing_produces_dataset():
    summary = {
        "total": {"metered_gross_usdc": 30.0, "platform_revenue_usdc": 3.0},
    }
    ds = extract_from_billing(summary)
    assert ds.latest("compute_usd_per_day") is not None
    # 30 / 30 days = $1/day
    assert ds.latest("compute_usd_per_day").value == pytest.approx(1.0)


def test_extract_from_billing_zero_gross_returns_empty():
    ds = extract_from_billing({"total": {"metered_gross_usdc": 0.0}})
    assert ds.latest("compute_usd_per_day") is None


def test_dataset_from_file(tmp_path):
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({
        "points": [
            {"metric": "compute_usd_per_day", "value": 0.04,
             "source": "audit", "observed_at": 100.0},
        ],
    }))
    ds = CalibrationDataset.from_file(path)
    assert len(ds.points) == 1
