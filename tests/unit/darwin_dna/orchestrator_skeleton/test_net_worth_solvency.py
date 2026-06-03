"""Net-worth solvency: a cash-poor but holding-rich Darwin is not bankrupted
(doc 05 §Treasury; audit "wrongly bankrupted while holding inventory").

check_death now tests net worth (cash + marked holdings); perceive marks the
holdings to USDC each heartbeat. With no holdings both behave exactly as the
old cash-only world.
"""

from __future__ import annotations

import types

import pytest

from darwin_dna.hyperparameters import DEFAULT_HYPERPARAMETERS
from darwin_dna.machinery.audit_logger import AuditLogger
from darwin_dna.machinery.holdings import Holdings, StaticPriceOracle
from darwin_dna.orchestrator_skeleton.nodes import check_death, perceive
from darwin_dna.orchestrator_skeleton.state import OrchestratorState
from darwin_fakes.fake_platform import MemoryAuditSink

_NO_REVENUE = DEFAULT_HYPERPARAMETERS.revenue_grace_window_seconds + 1.0
_MIN = DEFAULT_HYPERPARAMETERS.minimum_viable_balance_usdc  # 1.0


def _ctx(**kw):
    sink = MemoryAuditSink()
    base = dict(audit=AuditLogger("d-1", sink), hp=DEFAULT_HYPERPARAMETERS,
                _sink=sink)
    base.update(kw)
    return types.SimpleNamespace(**base)


# -- check_death: net worth, not cash -------------------------------------
def test_cash_poor_but_holding_rich_survives():
    # $0.001 cash (below the $1 floor) + $1000 of marked holdings, no revenue
    # for the whole grace window — the OLD behaviour would kill this Darwin.
    state = OrchestratorState(
        balance_usdc=0.001, holdings_value_usdc=1000.0,
        seconds_since_revenue=_NO_REVENUE,
    )
    check_death.run(state, _ctx())
    assert state.alive is True
    assert state.route == "perceive"


def test_cash_poor_no_holdings_still_bankrupt():
    # Parity: no holdings -> net worth == cash -> dies exactly as before.
    state = OrchestratorState(
        balance_usdc=0.001, holdings_value_usdc=0.0,
        seconds_since_revenue=_NO_REVENUE,
    )
    check_death.run(state, _ctx())
    assert state.alive is False
    assert state.route == "__end__"
    assert "bankruptcy" in state.death_reason


def test_holdings_too_small_still_bankrupt_with_net_worth_message():
    state = OrchestratorState(
        balance_usdc=0.001, holdings_value_usdc=0.002,
        seconds_since_revenue=_NO_REVENUE,
    )
    check_death.run(state, _ctx())
    assert state.alive is False
    assert "net worth" in state.death_reason
    assert "holdings" in state.death_reason


def test_healthy_cash_survives_unchanged():
    state = OrchestratorState(
        balance_usdc=_MIN + 5.0, holdings_value_usdc=0.0,
        seconds_since_revenue=_NO_REVENUE,
    )
    check_death.run(state, _ctx())
    assert state.alive is True
    assert state.route == "perceive"


def test_within_grace_window_not_bankrupt_even_if_broke():
    # Net worth below the floor but revenue is recent -> grace, no death.
    state = OrchestratorState(
        balance_usdc=0.0, holdings_value_usdc=0.0, seconds_since_revenue=0.0,
    )
    check_death.run(state, _ctx())
    assert state.alive is True


# -- perceive: marks holdings to USDC -------------------------------------
def _perceive_ctx(holdings, oracle):
    sink = MemoryAuditSink()
    wallet = types.SimpleNamespace(balance_usdc=lambda: 0.5)
    escalation = types.SimpleNamespace(poll_responses=lambda _id: [])
    return types.SimpleNamespace(
        audit=AuditLogger("d-1", sink), hp=DEFAULT_HYPERPARAMETERS,
        wallet=wallet, ledger=None, catalog={}, clock=lambda: 0.0,
        holdings=holdings, price_oracle=oracle, darwin_id="d-1",
        escalation=escalation, _sink=sink,
    )


def test_perceive_marks_holdings_and_sets_net_worth():
    h = Holdings()
    h.record_buy("ETH", 2.0, 4000.0)
    ctx = _perceive_ctx(h, StaticPriceOracle({"ETH": 3000.0}))
    state = OrchestratorState()
    perceive.run(state, ctx)
    assert state.holdings_value_usdc == pytest.approx(6000.0)  # 2 * 3000
    assert state.net_worth_usdc == pytest.approx(0.5 + 6000.0)
    events = [r for r in ctx._sink.read_all()
              if r.get("kind") == "event:holdings.marked_to_market"]
    assert events, "expected a holdings.marked_to_market audit event"


def test_perceive_no_holdings_net_worth_equals_cash():
    ctx = _perceive_ctx(Holdings(), None)
    state = OrchestratorState()
    perceive.run(state, ctx)
    assert state.holdings_value_usdc == 0.0
    assert state.net_worth_usdc == pytest.approx(0.5)
    events = [r for r in ctx._sink.read_all()
              if r.get("kind") == "event:holdings.marked_to_market"]
    assert events == []  # no event when nothing is held


def test_perceive_unpriceable_holding_falls_back_to_cost_basis():
    h = Holdings()
    h.record_buy("OBSCURE", 100.0, 250.0)
    ctx = _perceive_ctx(h, StaticPriceOracle({}))  # no quote
    state = OrchestratorState()
    perceive.run(state, ctx)
    # Cost basis, not zero -> the Darwin keeps its inventory value.
    assert state.holdings_value_usdc == pytest.approx(250.0)
