"""Browser vision-mode fallback (doc 15).

When DOM exploration returns navigation_failed AND the substrate
advertises vision support, the cached substrate should retry once with
vision mode before letting the failure propagate to the neuron."""

from __future__ import annotations

import pytest

from darwin_platform.adapters.browser import BrowserUseClient
from darwin_platform.infra.browser_cache import (
    BrowserCacheService,
    CachedBrowserSubstrate,
)


class _StubAgent:
    """Stand-in for browser_use.Agent."""

    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = 0

    def run(self, prompt, **kwargs):
        self.calls += 1
        return self._outcome


class _Wallet:
    def __init__(self, balance=100.0):
        self.balance = balance
        self.debits = []

    def debit(self, amount, memo):
        if amount > self.balance:
            raise RuntimeError("nsf")
        self.balance -= amount
        self.debits.append((amount, memo))

    def balance_usdc(self): return self.balance


# ---------------------------------------------------------------------------
# BrowserUseClient.run_vision
# ---------------------------------------------------------------------------

def test_supports_vision_false_when_no_factory():
    c = BrowserUseClient(wallet=_Wallet(), browser_factory=lambda: _StubAgent({"ok": True}))
    assert c.supports_vision is False


def test_supports_vision_true_with_factory():
    c = BrowserUseClient(
        wallet=_Wallet(),
        browser_factory=lambda: _StubAgent({"ok": True}),
        vision_factory=lambda: _StubAgent({"ok": True}),
    )
    assert c.supports_vision is True


def test_run_vision_returns_via_vision_on_success():
    c = BrowserUseClient(
        wallet=_Wallet(),
        browser_factory=lambda: _StubAgent({"ok": True}),
        vision_factory=lambda: _StubAgent({"ok": True, "data": {"signed_up": True}}),
    )
    out = c.run_vision(["open https://x.com/", "fill form"])
    assert out["ok"] is True
    assert out["via"] == "vision"
    assert out["data"] == {"signed_up": True}


def test_run_vision_returns_via_vision_on_failure():
    c = BrowserUseClient(
        wallet=_Wallet(),
        browser_factory=lambda: _StubAgent({"ok": True}),
        vision_factory=lambda: _StubAgent({"ok": False, "detail": "still no DOM"}),
    )
    out = c.run_vision(["open x"])
    assert out["ok"] is False
    assert out["via"] == "vision"


def test_run_vision_respects_cost_cap():
    """Costs cap is honored upfront; wallet refuses → returns no-funds."""
    w = _Wallet(balance=0.01)  # only enough for the small DOM session, not vision
    c = BrowserUseClient(
        wallet=w,
        browser_factory=lambda: _StubAgent({"ok": True}),
        vision_factory=lambda: _StubAgent({"ok": True}),
        vision_cost_per_step=0.10,
    )
    out = c.run_vision(["open x"], max_steps=5, cost_cap_usdc=0.5)
    assert out["ok"] is False
    assert "no funds" in out["detail"]


def test_run_vision_zero_caps_refused_cleanly():
    c = BrowserUseClient(
        wallet=_Wallet(),
        browser_factory=lambda: _StubAgent({"ok": True}),
        vision_factory=lambda: _StubAgent({"ok": True}),
    )
    out = c.run_vision(["x"], max_steps=0, cost_cap_usdc=0.5)
    assert out["ok"] is False
    assert "caps misconfigured" in out["detail"]


def test_run_vision_no_factory_returns_not_configured():
    c = BrowserUseClient(
        wallet=_Wallet(),
        browser_factory=lambda: _StubAgent({"ok": True}),
    )
    out = c.run_vision(["x"])
    assert out["ok"] is False
    assert "not configured" in out["detail"]


# ---------------------------------------------------------------------------
# CachedBrowserSubstrate vision fallback
# ---------------------------------------------------------------------------

class _VisionCapableSubstrate:
    """Stub substrate with supports_vision + run_vision."""

    supports_vision = True

    def __init__(self, dom_outcomes, vision_outcomes):
        self.dom_outcomes = list(dom_outcomes)
        self.vision_outcomes = list(vision_outcomes)
        self.dom_calls = 0
        self.vision_calls = 0

    def run(self, steps):
        self.dom_calls += 1
        return self.dom_outcomes.pop(0)

    def run_vision(self, steps, *, max_steps, cost_cap_usdc):
        self.vision_calls += 1
        return self.vision_outcomes.pop(0)


def test_vision_fallback_triggered_on_nav_failed():
    """DOM exploration fails → vision retry → vision success surfaces."""
    sub = _VisionCapableSubstrate(
        dom_outcomes=[{"ok": False, "detail": "canvas-only", "cost_usdc": 0.05}],
        vision_outcomes=[{"ok": True, "data": {"published": True},
                          "cost_usdc": 0.3, "via": "vision"}],
    )
    cache = BrowserCacheService(promote_after_successes=10)
    w = CachedBrowserSubstrate(
        exploration=sub, cache=cache, darwin_id="d-1",
    )
    out = w.run(["open https://canvas.example/"])
    assert out["ok"] is True
    assert out["via"] == "vision"
    assert sub.dom_calls == 1
    assert sub.vision_calls == 1
    # And the session log carries a vision record.
    sessions = cache.sessions(darwin_id="d-1")
    via_values = {s.via for s in sessions}
    assert "exploration" in via_values
    assert "vision" in via_values


def test_vision_fallback_not_triggered_on_success():
    sub = _VisionCapableSubstrate(
        dom_outcomes=[{"ok": True, "data": {}, "cost_usdc": 0.05}],
        vision_outcomes=[],
    )
    cache = BrowserCacheService()
    w = CachedBrowserSubstrate(
        exploration=sub, cache=cache, darwin_id="d-1",
    )
    w.run(["open x"])
    assert sub.vision_calls == 0


def test_vision_fallback_not_triggered_on_captcha():
    """A captcha should escalate, NOT try vision (vision wouldn't help)."""
    sub = _VisionCapableSubstrate(
        dom_outcomes=[{"blocked": "captcha", "detail": "hcaptcha", "cost_usdc": 0.05}],
        vision_outcomes=[],
    )
    cache = BrowserCacheService()
    w = CachedBrowserSubstrate(
        exploration=sub, cache=cache, darwin_id="d-1",
    )
    out = w.run(["open x"])
    assert out["blocked"] == "captcha"
    assert sub.vision_calls == 0


def test_vision_fallback_not_triggered_when_substrate_unsupported():
    class _NoVision:
        supports_vision = False
        def run(self, steps):
            return {"ok": False, "detail": "no DOM", "cost_usdc": 0.05}

    sub = _NoVision()
    cache = BrowserCacheService()
    w = CachedBrowserSubstrate(
        exploration=sub, cache=cache, darwin_id="d-1",
    )
    out = w.run(["open x"])
    # Returns the original failure unchanged.
    assert out["ok"] is False
    assert "via" not in out  # the original outcome didn't tag itself


def test_vision_fallback_disabled_flag():
    sub = _VisionCapableSubstrate(
        dom_outcomes=[{"ok": False, "detail": "x", "cost_usdc": 0.05}],
        vision_outcomes=[{"ok": True, "data": {}, "cost_usdc": 0.3}],
    )
    cache = BrowserCacheService()
    w = CachedBrowserSubstrate(
        exploration=sub, cache=cache, darwin_id="d-1",
        vision_fallback_enabled=False,
    )
    out = w.run(["open x"])
    assert out["ok"] is False
    assert sub.vision_calls == 0
