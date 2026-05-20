"""Tests for the Stagehand replay substrate (doc 15)."""

from __future__ import annotations

import pytest

from darwin_platform.adapters.browser import BrowserClient
from darwin_platform.adapters.stagehand import (
    InMemoryStagehandClient,
    StagehandReplayClient,
)
from darwin_platform.browser_cache import (
    BrowserCacheService,
    CachedBrowserSubstrate,
)


class _Wallet:
    def __init__(self, balance=10.0):
        self.balance = balance
        self.debits = []
    def debit(self, amount, memo):
        if amount > self.balance:
            raise RuntimeError("nsf")
        self.balance -= amount
        self.debits.append((amount, memo))
    def balance_usdc(self): return self.balance


# ---------------------------------------------------------------------------
# StagehandReplayClient
# ---------------------------------------------------------------------------

def test_protocol_satisfied():
    inner = InMemoryStagehandClient([{"success": True, "data": {}}])
    client = StagehandReplayClient(
        wallet=_Wallet(), stagehand_factory=lambda: inner,
    )
    assert isinstance(client, BrowserClient)


def test_replay_success_returns_via_tag():
    inner = InMemoryStagehandClient([{"success": True, "data": {"a": 1}}])
    client = StagehandReplayClient(
        wallet=_Wallet(), stagehand_factory=lambda: inner,
    )
    out = client.run(["open x"])
    assert out["ok"] is True
    assert out["via"] == "stagehand_replay"
    assert out["data"]["a"] == 1


def test_replay_failure_returns_navigation_failed():
    inner = InMemoryStagehandClient([{"success": False, "error": "selector missing"}])
    client = StagehandReplayClient(
        wallet=_Wallet(), stagehand_factory=lambda: inner,
    )
    out = client.run(["open x"])
    assert out["ok"] is False
    assert out["via"] == "stagehand_replay"
    assert "selector" in out["detail"]


def test_replay_captcha_surfaces_blocked():
    """Stagehand can't solve captchas; a captcha-detail must surface
    as blocked so the cached substrate's branches kick in."""
    inner = InMemoryStagehandClient([
        {"success": False, "error": "hCaptcha required"},
    ])
    client = StagehandReplayClient(
        wallet=_Wallet(), stagehand_factory=lambda: inner,
    )
    out = client.run(["open x"])
    assert out["blocked"] == "captcha"


def test_replay_wallet_refusal_returns_no_funds():
    inner = InMemoryStagehandClient([{"success": True, "data": {}}])
    client = StagehandReplayClient(
        wallet=_Wallet(balance=0.0), stagehand_factory=lambda: inner,
    )
    out = client.run(["x"])
    assert out["ok"] is False
    assert "wallet refused" in out["detail"]


def test_replay_factory_init_failure_returns_error():
    def _broken_factory():
        raise RuntimeError("missing API key")

    client = StagehandReplayClient(
        wallet=_Wallet(), stagehand_factory=_broken_factory,
    )
    out = client.run(["x"])
    assert out["ok"] is False
    assert "stagehand init failed" in out["detail"]


def test_replay_cost_default_is_cheap():
    """Cached replay should be ~100x cheaper than Browser-Use
    exploration ($0.001 default vs $0.05+ for BrowserUseClient)."""
    inner = InMemoryStagehandClient([{"success": True, "data": {}}])
    w = _Wallet(balance=10.0)
    client = StagehandReplayClient(wallet=w, stagehand_factory=lambda: inner)
    client.run(["x"])
    assert w.debits[0][0] == pytest.approx(0.001)


def test_replay_empty_factory_refused():
    with pytest.raises(ValueError):
        StagehandReplayClient(wallet=_Wallet(), stagehand_factory=None)


# ---------------------------------------------------------------------------
# Wiring into CachedBrowserSubstrate as the cheap replay path
# ---------------------------------------------------------------------------

class _ExplorationStub:
    """Browser-Use-shaped substrate that records calls + uses higher cost."""

    def __init__(self):
        self.calls = []
    def run(self, steps):
        self.calls.append(list(steps))
        return {"ok": True, "cost_usdc": 0.05}


def test_cached_substrate_uses_stagehand_replay_when_promoted():
    """When a signature is promoted, the cache substrate uses the
    replay client (cheap) instead of the exploration substrate
    (expensive)."""
    exploration = _ExplorationStub()
    inner_stagehand = InMemoryStagehandClient(
        [{"success": True, "data": {"replayed": True}}] * 5
    )
    replay = StagehandReplayClient(
        wallet=_Wallet(), stagehand_factory=lambda: inner_stagehand,
    )
    cache = BrowserCacheService(promote_after_successes=2)
    wrapper = CachedBrowserSubstrate(
        exploration=exploration, replay=replay, cache=cache, darwin_id="d-1",
    )
    steps = ["open https://blog.example/post"]
    # Two exploration successes promote.
    wrapper.run(steps)
    wrapper.run(steps)
    # Third call is the cached replay via Stagehand.
    third = wrapper.run(steps)
    assert third["via"] in ("cached", "stagehand_replay")
    # The Stagehand inner client was called once (the third run).
    assert len(inner_stagehand.calls) == 1
    # Exploration was called exactly twice (the two successes that
    # earned the promotion); the third call went to the replay path.
    assert len(exploration.calls) == 2
