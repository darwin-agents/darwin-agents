"""Tests for the browser cache + promotion engine (docs/engineering/15).

Covers:
- Signature computation: structural hash, value-invariant.
- Promotion after N successes.
- Demotion after M consecutive cached failures.
- Per-session cost attribution (darwin / neuron / URL / via).
- Drop-in wrapper preserves the substrate's outcome shape.
- Supervisor wires the wrapper at spawn time.
- HTTP endpoints serve session log + fleet stats.
"""

from __future__ import annotations

import time

import pytest

from darwin_platform.browser_cache import (
    BrowserCacheService,
    BrowserSessionRecord,
    CachedBrowserSubstrate,
)


class _RecordingSubstrate:
    """Stub substrate. Returns whatever the test pre-loads, records calls."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def run(self, steps):
        self.calls.append(list(steps))
        if not self._outcomes:
            return {"ok": True, "cost_usdc": 0.05}
        return self._outcomes.pop(0)


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------

def test_signature_stable_across_values():
    """Same actions on same host, different form values → same signature."""
    a = ["open https://example.com/post", "fill field=title value=A"]
    b = ["open https://example.com/post", "fill field=title value=B"]
    assert BrowserCacheService.signature(a) == BrowserCacheService.signature(b)


def test_signature_differs_on_host_change():
    a = ["open https://example.com/post"]
    b = ["open https://other.com/post"]
    assert BrowserCacheService.signature(a) != BrowserCacheService.signature(b)


def test_signature_differs_on_action_kind():
    a = ["open https://example.com"]
    b = ["click https://example.com"]
    assert BrowserCacheService.signature(a) != BrowserCacheService.signature(b)


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------

def test_promotion_after_n_successes():
    cache = BrowserCacheService(promote_after_successes=3)
    sub = _RecordingSubstrate([{"ok": True, "cost_usdc": 0.05}] * 5)
    wrapper = CachedBrowserSubstrate(
        exploration=sub, cache=cache, darwin_id="d-1",
    )
    steps = ["open https://example.com/blog"]
    # First three calls promote
    for i in range(3):
        wrapper.run(steps)
    sig = cache.signature(steps)
    assert cache.is_cached(sig) is True
    # Fourth call uses cached path (no separate replay substrate, so the
    # exploration substrate is reused but the session is tagged cached).
    wrapper.run(steps)
    sessions = cache.sessions(darwin_id="d-1")
    assert sessions[-1].via == "cached"


def test_promotion_uses_cached_steps_for_replay():
    cache = BrowserCacheService(promote_after_successes=2)
    sub = _RecordingSubstrate([{"ok": True, "cost_usdc": 0.05}] * 5)
    wrapper = CachedBrowserSubstrate(
        exploration=sub, cache=cache, darwin_id="d-1",
    )
    steps = ["open https://example.com/blog"]
    wrapper.run(steps)
    wrapper.run(steps)  # promotes
    sig = cache.signature(steps)
    assert cache.cached_steps(sig) == steps


def test_failed_exploration_does_not_advance_promotion():
    cache = BrowserCacheService(promote_after_successes=2)
    sub = _RecordingSubstrate([
        {"ok": False, "blocked": "captcha"},
        {"ok": False, "blocked": "captcha"},
    ])
    wrapper = CachedBrowserSubstrate(
        exploration=sub, cache=cache, darwin_id="d-1",
    )
    steps = ["open https://example.com/blog"]
    wrapper.run(steps)
    wrapper.run(steps)
    sig = cache.signature(steps)
    # Still not promoted — failures don't count.
    assert cache.is_cached(sig) is False


def test_cached_cost_is_fraction_of_exploration():
    cache = BrowserCacheService(promote_after_successes=2)
    sub = _RecordingSubstrate([{"ok": True, "cost_usdc": 1.0}] * 4)
    wrapper = CachedBrowserSubstrate(
        exploration=sub, cache=cache, darwin_id="d-1",
        cached_cost_multiplier=0.1,
    )
    steps = ["open https://example.com/blog"]
    wrapper.run(steps)
    wrapper.run(steps)
    wrapper.run(steps)  # cached
    sessions = cache.sessions(darwin_id="d-1")
    # The third call's recorded cost is 0.1 (1.0 * cached_cost_multiplier).
    assert sessions[-1].via == "cached"
    assert sessions[-1].cost_usdc == pytest.approx(0.1, abs=1e-6)


# ---------------------------------------------------------------------------
# Demotion
# ---------------------------------------------------------------------------

def test_demotion_after_m_cached_failures():
    cache = BrowserCacheService(
        promote_after_successes=1, demote_after_failures=2,
    )
    sub = _RecordingSubstrate([
        {"ok": True, "cost_usdc": 0.05},   # exploration → promote
        {"ok": False, "blocked": "captcha"},  # cached fail 1
        {"ok": False, "blocked": "captcha"},  # cached fail 2 → demote
    ])
    wrapper = CachedBrowserSubstrate(
        exploration=sub, cache=cache, darwin_id="d-1",
    )
    steps = ["open https://example.com/blog"]
    wrapper.run(steps)  # promote
    sig = cache.signature(steps)
    assert cache.is_cached(sig)
    wrapper.run(steps)  # cached fail 1
    wrapper.run(steps)  # cached fail 2 → demote
    assert cache.is_cached(sig) is False


# ---------------------------------------------------------------------------
# Per-session cost attribution
# ---------------------------------------------------------------------------

def test_sessions_carry_attribution():
    cache = BrowserCacheService(promote_after_successes=10)
    sub = _RecordingSubstrate([
        {"ok": True, "cost_usdc": 0.03},
        {"ok": False, "blocked": "captcha", "cost_usdc": 0.04},
    ])
    wrapper = CachedBrowserSubstrate(
        exploration=sub, cache=cache, darwin_id="d-7",
        neuron="browser.operator",
    )
    wrapper.run(["open https://blog.example/post"])
    wrapper.run(["open https://other.example/x"])
    sessions = cache.sessions(darwin_id="d-7")
    assert len(sessions) == 2
    assert sessions[0].url == "https://blog.example/post"
    assert sessions[0].neuron == "browser.operator"
    assert sessions[0].darwin_id == "d-7"
    assert sessions[1].status == "blocked"


def test_stats_aggregates_per_darwin():
    cache = BrowserCacheService(promote_after_successes=10)
    sub_a = _RecordingSubstrate([{"ok": True, "cost_usdc": 0.05}] * 3)
    sub_b = _RecordingSubstrate([{"ok": True, "cost_usdc": 0.05}] * 2)
    wa = CachedBrowserSubstrate(exploration=sub_a, cache=cache, darwin_id="d-a")
    wb = CachedBrowserSubstrate(exploration=sub_b, cache=cache, darwin_id="d-b")
    for _ in range(3):
        wa.run(["open https://x.com/"])
    for _ in range(2):
        wb.run(["open https://y.com/"])
    stats = cache.stats()
    assert stats["total_sessions"] == 5
    assert stats["darwins"]["d-a"]["sessions"] == 3
    assert stats["darwins"]["d-b"]["sessions"] == 2


# ---------------------------------------------------------------------------
# Supervisor wiring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_supervisor_wraps_browser_substrate():
    from darwin_platform.supervisor import Supervisor

    # Disable the throttle wrapper so the outer wrapper is CachedBrowserSubstrate.
    sup = Supervisor(resource_throttle_enabled=False)
    status = await sup.spawn(opening_usdc=50.0)
    h = sup._darwins[status.darwin_id]
    # The browser is now the cache wrapper, not the raw FakeBrowser.
    assert isinstance(h.stack.browser, CachedBrowserSubstrate)
    # Drive a session via the wrapped substrate; should appear in stats.
    h.stack.browser.run(["open https://blog.example/post"])
    stats = sup.browser_stats()
    assert stats["total_sessions"] == 1
    sessions = sup.browser_sessions(status.darwin_id)
    assert len(sessions) == 1
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_browser_cache_can_be_disabled():
    """For tests that depend on raw FakeBrowser semantics."""
    from darwin_fakes import FakeBrowser
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor(
        browser_cache_enabled=False, resource_throttle_enabled=False,
    )
    status = await sup.spawn(opening_usdc=50.0)
    h = sup._darwins[status.darwin_id]
    assert isinstance(h.stack.browser, FakeBrowser)
    await sup.shutdown(kill_running=True)


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http_sessions_and_stats():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    did = status.darwin_id
    h = sup._darwins[did]
    h.stack.browser.run(["open https://blog.example/post"])
    h.stack.browser.run(["open https://other.example/path"])

    app = build_app(sup)
    with TestClient(app) as client:
        r = client.get(f"/darwins/{did}/browser/sessions")
        assert r.status_code == 200
        assert len(r.json()["sessions"]) == 2

        r2 = client.get("/platform/browser/stats")
        assert r2.status_code == 200
        body = r2.json()
        assert body["total_sessions"] == 2
        assert did in body["darwins"]

    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_http_404_unknown_darwin():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.get("/darwins/ghost/browser/sessions")
        assert r.status_code == 404
    await sup.shutdown(kill_running=True)
