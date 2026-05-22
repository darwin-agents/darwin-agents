"""Tests for the Phase 0 soak-test assertion logic (docs/engineering/20).

The ``run_soak`` driver itself needs a live server (exercised manually +
by the operator); these tests cover the pure, CI-reusable parts: the
duration parser and the six per-criterion assertion functions. Audit
events are built through the *real* :class:`AuditLogger` so criterion 3
verifies a genuine hash chain, not a hand-rolled fake.
"""

from __future__ import annotations

import pytest

from darwin_dna.machinery.audit_logger import AuditLogger
from darwin_fakes.fake_platform import MemoryAuditSink

from tools.soak.assertions import (
    criterion_1_uptime,
    criterion_2_neuron_dispatches,
    criterion_3_hash_chain,
    criterion_4_ledger_reconciles,
    criterion_5_kill_latency,
    criterion_6_replayable,
)
from tools.soak.config import SoakConfig, parse_duration


# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("30s", 30.0), ("90m", 5400.0), ("24h", 86400.0),
        ("2d", 172800.0), ("1.5h", 5400.0), ("120", 120.0),
        ("  45s ", 45.0),
    ],
)
def test_parse_duration_accepts_units(text, expected):
    assert parse_duration(text) == pytest.approx(expected)


@pytest.mark.parametrize("bad", ["", "abc", "10x", "-5s", "0", "h"])
def test_parse_duration_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


# ---------------------------------------------------------------------------
# audit-event builders (real hash chain)
# ---------------------------------------------------------------------------

class _Chain:
    """Builds a genuine hash-chained event list via the real AuditLogger."""

    def __init__(self) -> None:
        self._sink = MemoryAuditSink()
        self._log = AuditLogger("d-soak", self._sink)

    def birth(self) -> "_Chain":
        self._log.log_event("birth", {"darwin_id": "d-soak"})
        return self

    def perceive(self, tick: int) -> "_Chain":
        self._log.log_event("perceive", {"tick": tick, "balance_usdc": 100.0})
        return self

    def reconciled(self, drift: float = 0.0) -> "_Chain":
        self._log.log_event(
            "ledger.reconciled", {"drift_usdc": drift, "treasury_usdc": 100.0},
        )
        return self

    def dispatch(self, neuron: str, *, status: str = "ok") -> "_Chain":
        iid = self._log.log_intent(f"neuron.{neuron}", {"neuron": neuron})
        self._log.log_outcome(iid, status, {"neuron": neuron})
        return self

    def events(self) -> list[dict]:
        return self._sink.read_all()


# ---------------------------------------------------------------------------
# Criterion 1 — uptime
# ---------------------------------------------------------------------------

def _obs(elapsed, *, http_ok=True, healthz_ok=True, alive=True, tick=0):
    return {
        "elapsed": elapsed, "http_ok": http_ok,
        "healthz_ok": healthz_ok, "alive": alive, "tick": tick,
    }


def test_criterion_1_passes_on_clean_run():
    cfg = SoakConfig(duration_seconds=100.0)
    obs = [_obs(0, tick=1), _obs(50, tick=500), _obs(100, tick=1000)]
    r = criterion_1_uptime(obs, cfg)
    assert r.ok and r.number == 1


def test_criterion_1_fails_on_failed_poll():
    cfg = SoakConfig(duration_seconds=100.0)
    obs = [_obs(0, tick=1), _obs(50, http_ok=False, tick=500),
           _obs(100, tick=1000)]
    r = criterion_1_uptime(obs, cfg)
    assert not r.ok
    assert "failed status polls" in r.detail


def test_criterion_1_fails_when_tick_frozen():
    cfg = SoakConfig(duration_seconds=100.0)
    obs = [_obs(0, tick=7), _obs(50, tick=7), _obs(100, tick=7)]
    r = criterion_1_uptime(obs, cfg)
    assert not r.ok
    assert "tick did not advance" in r.detail


# ---------------------------------------------------------------------------
# Criterion 2 — neuron dispatches
# ---------------------------------------------------------------------------

def test_criterion_2_passes_at_threshold():
    cfg = SoakConfig(min_neuron_dispatches=50, min_dispatch_success_rate=0.80)
    chain = _Chain().birth()
    for _ in range(55):
        chain.dispatch("content.writer", status="ok")
    r = criterion_2_neuron_dispatches(chain.events(), cfg)
    assert r.ok
    assert "55 dispatches" in r.detail


def test_criterion_2_fails_below_dispatch_floor():
    cfg = SoakConfig(min_neuron_dispatches=50)
    chain = _Chain().birth()
    for _ in range(10):
        chain.dispatch("content.writer")
    r = criterion_2_neuron_dispatches(chain.events(), cfg)
    assert not r.ok
    assert "10 dispatches" in r.detail


def test_criterion_2_fails_below_success_rate():
    cfg = SoakConfig(min_neuron_dispatches=50, min_dispatch_success_rate=0.80)
    chain = _Chain().birth()
    for i in range(60):
        # 50% success — well under the 80% bar.
        chain.dispatch("content.writer", status="ok" if i % 2 else "fail")
    r = criterion_2_neuron_dispatches(chain.events(), cfg)
    assert not r.ok
    assert "50.0% success" in r.detail


# ---------------------------------------------------------------------------
# Criterion 3 — hash chain
# ---------------------------------------------------------------------------

def test_criterion_3_passes_on_intact_chain():
    events = _Chain().birth().perceive(1).reconciled().events()
    r = criterion_3_hash_chain(events, total=len(events))
    assert r.ok


def test_criterion_3_fails_on_tampered_record():
    events = _Chain().birth().perceive(1).reconciled().events()
    # Flip a payload field without recomputing the hash → chain breaks.
    events[1]["payload"]["tick"] = 9999
    r = criterion_3_hash_chain(events, total=len(events))
    assert not r.ok
    assert "chain" in r.detail.lower()


def test_criterion_3_fails_on_incomplete_fetch():
    events = _Chain().birth().perceive(1).events()
    # total says there are more events than we fetched.
    r = criterion_3_hash_chain(events, total=len(events) + 100)
    assert not r.ok
    assert "incomplete" in r.detail


# ---------------------------------------------------------------------------
# Criterion 4 — ledger reconciles
# ---------------------------------------------------------------------------

def test_criterion_4_passes_on_zero_drift():
    cfg = SoakConfig()
    events = _Chain().birth().reconciled(0.0).reconciled(0.0).events()
    r = criterion_4_ledger_reconciles(events, cfg)
    assert r.ok


def test_criterion_4_fails_on_drift():
    cfg = SoakConfig()
    events = _Chain().birth().reconciled(0.0).reconciled(0.5).events()
    r = criterion_4_ledger_reconciles(events, cfg)
    assert not r.ok
    assert "drifted" in r.detail


def test_criterion_4_fails_when_no_reconciliations():
    cfg = SoakConfig()
    events = _Chain().birth().perceive(1).events()
    r = criterion_4_ledger_reconciles(events, cfg)
    assert not r.ok


# ---------------------------------------------------------------------------
# Criterion 5 — kill latency
# ---------------------------------------------------------------------------

def test_criterion_5_passes_under_budget():
    r = criterion_5_kill_latency(0.3, SoakConfig(kill_latency_budget_seconds=5.0))
    assert r.ok


def test_criterion_5_fails_over_budget():
    r = criterion_5_kill_latency(7.0, SoakConfig(kill_latency_budget_seconds=5.0))
    assert not r.ok


def test_criterion_5_fails_on_timeout():
    r = criterion_5_kill_latency(None, SoakConfig())
    assert not r.ok
    assert "did not confirm dead" in r.detail


# ---------------------------------------------------------------------------
# Criterion 6 — replayable
# ---------------------------------------------------------------------------

def test_criterion_6_passes_on_complete_log():
    events = (
        _Chain().birth().perceive(1).perceive(2).perceive(3).reconciled()
        .events()
    )
    r = criterion_6_replayable(events, final_status={"tick": 3})
    assert r.ok


def test_criterion_6_fails_without_birth_at_seq_0():
    # Drop the birth event entirely.
    events = _Chain().birth().perceive(1).events()[1:]
    r = criterion_6_replayable(events, final_status={"tick": 1})
    assert not r.ok


def test_criterion_6_fails_on_seq_gap():
    events = _Chain().birth().perceive(1).perceive(2).events()
    del events[1]  # punch a hole in the seq sequence
    r = criterion_6_replayable(events, final_status={"tick": 2})
    assert not r.ok
    assert "contiguous" in r.detail


def test_criterion_6_fails_when_replayed_tick_mismatches():
    events = _Chain().birth().perceive(1).perceive(2).events()
    # Final status claims tick 999 but the log only reached 2.
    r = criterion_6_replayable(events, final_status={"tick": 999})
    assert not r.ok
    assert "does not match" in r.detail
