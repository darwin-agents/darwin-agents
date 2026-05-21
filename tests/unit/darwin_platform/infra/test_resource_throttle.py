"""Tests for level-2 throttle on non-LLM resources (doc 09 level 2)."""

from __future__ import annotations

import pytest

from darwin_platform.infra.throttle import (
    THROTTLE_KIND_BROWSER,
    THROTTLE_KIND_EGRESS,
    THROTTLE_KIND_SANDBOX,
    ThrottleError,
    ThrottleService,
    ThrottledBrowserSubstrate,
    ThrottledEgressClient,
    ThrottledSandboxSubstrate,
)


def test_unknown_kind_rejected():
    s = ThrottleService()
    with pytest.raises(ThrottleError):
        s.set(darwin_id="d-1", kind="cpu", max_daily_calls=10)


def test_negative_limit_rejected():
    s = ThrottleService()
    with pytest.raises(ThrottleError):
        s.set(darwin_id="d-1", kind="browser", max_daily_calls=-1)


def test_no_throttle_means_unlimited():
    s = ThrottleService()
    for _ in range(100):
        assert s.consume(darwin_id="d-1", kind="browser")


def test_throttle_blocks_after_cap():
    s = ThrottleService()
    s.set(darwin_id="d-1", kind="browser", max_daily_calls=3)
    assert s.consume(darwin_id="d-1", kind="browser")
    assert s.consume(darwin_id="d-1", kind="browser")
    assert s.consume(darwin_id="d-1", kind="browser")
    assert s.consume(darwin_id="d-1", kind="browser") is False
    assert s.used_today(darwin_id="d-1", kind="browser") == 3


def test_throttles_are_per_kind():
    s = ThrottleService()
    s.set(darwin_id="d-1", kind="browser", max_daily_calls=1)
    s.consume(darwin_id="d-1", kind="browser")
    # sandbox unaffected
    assert s.consume(darwin_id="d-1", kind="sandbox")


def test_throttles_are_per_darwin():
    s = ThrottleService()
    s.set(darwin_id="d-1", kind="browser", max_daily_calls=1)
    s.consume(darwin_id="d-1", kind="browser")
    # other Darwin still allowed
    assert s.consume(darwin_id="d-2", kind="browser")


def test_clear_specific_kind():
    s = ThrottleService()
    s.set(darwin_id="d-1", kind="browser", max_daily_calls=1)
    s.set(darwin_id="d-1", kind="sandbox", max_daily_calls=1)
    removed = s.clear(darwin_id="d-1", kind="browser")
    assert len(removed) == 1
    assert removed[0].kind == "browser"
    # browser cleared, sandbox still installed
    assert s.list_for("d-1")[0].kind == "sandbox"


def test_clear_all():
    s = ThrottleService()
    s.set(darwin_id="d-1", kind="browser", max_daily_calls=1)
    s.set(darwin_id="d-1", kind="sandbox", max_daily_calls=1)
    s.clear(darwin_id="d-1")
    assert s.list_for("d-1") == []


# ---------------------------------------------------------------------------
# Substrate wrappers
# ---------------------------------------------------------------------------

class _BrowserStub:
    def __init__(self):
        self.calls = 0

    def run(self, steps):
        self.calls += 1
        return {"ok": True, "cost_usdc": 0.05}


def test_browser_wrapper_refuses_after_cap():
    svc = ThrottleService()
    svc.set(darwin_id="d-1", kind=THROTTLE_KIND_BROWSER, max_daily_calls=2)
    sub = _BrowserStub()
    w = ThrottledBrowserSubstrate(inner=sub, throttle=svc, darwin_id="d-1")
    assert w.run(["x"]).get("ok") is True
    assert w.run(["x"]).get("ok") is True
    refused = w.run(["x"])
    assert refused["ok"] is False
    assert refused["blocked"] == "throttled"
    # Inner substrate was only called twice — the third was refused
    assert sub.calls == 2


class _SandboxStub:
    def __init__(self):
        self.calls = 0

    def run_code(self, *, language, code, timeout_seconds=30.0):
        self.calls += 1
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": "",
                "cost_usdc": 0.01}


def test_sandbox_wrapper_refuses_after_cap():
    svc = ThrottleService()
    svc.set(darwin_id="d-1", kind=THROTTLE_KIND_SANDBOX, max_daily_calls=1)
    sub = _SandboxStub()
    w = ThrottledSandboxSubstrate(inner=sub, throttle=svc, darwin_id="d-1")
    assert w.run_code(language="python", code="print(1)").get("ok") is True
    refused = w.run_code(language="python", code="print(2)")
    assert refused["ok"] is False
    assert refused["exit_code"] == -1
    assert sub.calls == 1


class _EgressStub:
    def __init__(self):
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        import types
        return types.SimpleNamespace(status_code=200, headers={}, text="")


def test_egress_wrapper_refuses_after_cap():
    svc = ThrottleService()
    svc.set(darwin_id="d-1", kind=THROTTLE_KIND_EGRESS, max_daily_calls=1)
    sub = _EgressStub()
    w = ThrottledEgressClient(inner=sub, throttle=svc, darwin_id="d-1")
    assert w.request("GET", "https://x.com").status_code == 200
    assert w.request("GET", "https://x.com").status_code == 429
    assert sub.calls == 1


# ---------------------------------------------------------------------------
# Supervisor wiring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_supervisor_wraps_substrates():
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    h = sup._darwins[status.darwin_id]
    # All three substrates are wrapped (the FakeBrowser → cached → throttled).
    assert isinstance(h.stack.browser, ThrottledBrowserSubstrate)
    assert isinstance(h.stack.sandbox, ThrottledSandboxSubstrate)
    # No x402 configured by default, so it's None
    assert h.stack.x402_client is None
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_throttle_blocks_browser():
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    sup.set_resource_throttle(
        status.darwin_id, kind="browser", max_daily_calls=2,
        actor="alice",
    )
    h = sup._darwins[status.darwin_id]
    h.stack.browser.run(["open https://x.com/"])
    h.stack.browser.run(["open https://x.com/"])
    third = h.stack.browser.run(["open https://x.com/"])
    assert third["ok"] is False
    assert third["blocked"] == "throttled"
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_throttle_audits():
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    sup.set_resource_throttle(
        status.darwin_id, kind="sandbox", max_daily_calls=5, actor="alice",
    )
    entries = sup._audit.entries(action="set_resource_throttle")
    assert len(entries) == 1
    assert entries[0].metadata["kind"] == "sandbox"
    sup.clear_resource_throttle(status.darwin_id, kind="sandbox", actor="alice")
    cleared = sup._audit.entries(action="clear_resource_throttle")
    assert len(cleared) == 1
    await sup.shutdown(kill_running=True)


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http_set_get_clear():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    did = status.darwin_id
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.post(
            f"/platform/darwins/{did}/resource_throttle",
            json={"kind": "browser", "max_daily_calls": 10},
        )
        assert r.status_code == 200
        assert r.json()["kind"] == "browser"

        r2 = client.get(f"/platform/darwins/{did}/resource_throttle")
        assert len(r2.json()["throttles"]) == 1

        r3 = client.delete(
            f"/platform/darwins/{did}/resource_throttle?kind=browser"
        )
        assert r3.status_code == 200

        r4 = client.get(f"/platform/darwins/{did}/resource_throttle")
        assert r4.json()["throttles"] == []

    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_http_422_on_bad_kind():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.post(
            f"/platform/darwins/{status.darwin_id}/resource_throttle",
            json={"kind": "cpu", "max_daily_calls": 1},
        )
        assert r.status_code == 422
    await sup.shutdown(kill_running=True)
