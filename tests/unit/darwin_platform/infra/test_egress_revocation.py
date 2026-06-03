"""Tests for the egress kill-switch + serve-entrypoint bandwidth wiring
(docs/engineering/08, 09, 24).

- RevocationRegistry: revoke/restore/idempotency.
- /proxy refuses a revoked Darwin's traffic BEFORE the allowlist + before any
  upstream round-trip (the parallel egress cut).
- /revoke + /unrevoke + /revoked HTTP verbs.
- build_parser exposes the bandwidth-quota flags the serve entrypoint now wires.
- Supervisor.kill cuts egress at the proxy via the injected revoker.
"""

from __future__ import annotations

import pytest

from darwin_platform.infra.egress_proxy import (
    Allowlist,
    AllowlistEntry,
    RevocationRegistry,
    _Audit,
    _build_app,
    build_parser,
)


def _client(revocations=None):
    from fastapi.testclient import TestClient

    allowlist = Allowlist(entries=[
        AllowlistEntry(host_suffix="example.com", methods=("GET", "POST")),
    ])
    app = _build_app(allowlist, _Audit(), revocations=revocations)
    return TestClient(app)


# ---------------------------------------------------------------------------
# RevocationRegistry
# ---------------------------------------------------------------------------

def test_registry_revoke_and_restore():
    r = RevocationRegistry()
    assert r.is_revoked("d-1") is False
    r.revoke("d-1")
    assert r.is_revoked("d-1") is True
    r.revoke("d-1")  # idempotent
    assert r.snapshot() == ["d-1"]
    r.restore("d-1")
    assert r.is_revoked("d-1") is False


def test_registry_ignores_empty_id():
    r = RevocationRegistry()
    r.revoke("")
    assert r.snapshot() == []


# ---------------------------------------------------------------------------
# HTTP verbs + the proxy revocation gate
# ---------------------------------------------------------------------------

def test_revoke_then_proxy_is_cut():
    reg = RevocationRegistry()
    client = _client(revocations=reg)
    # Revoke the Darwin.
    resp = client.post("/revoke", json={"darwin_id": "d-killed"})
    assert resp.status_code == 200
    assert resp.json()["revoked"] == "d-killed"
    # Its outbound traffic is now refused BEFORE the allowlist / upstream call.
    resp = client.post(
        "/proxy",
        json={"method": "GET", "url": "https://example.com/x"},
        headers={"x-darwin-id": "d-killed"},
    )
    assert resp.status_code == 403
    assert "revoked" in resp.json()["detail"].lower()


def test_unrevoke_restores_and_revoked_listing():
    reg = RevocationRegistry()
    client = _client(revocations=reg)
    client.post("/revoke", json={"darwin_id": "d-1"})
    assert client.get("/revoked").json()["revoked"] == ["d-1"]
    client.post("/unrevoke", json={"darwin_id": "d-1"})
    assert client.get("/revoked").json()["revoked"] == []


def test_revoke_disabled_when_registry_absent():
    client = _client(revocations=None)
    assert client.get("/revoked").json()["enabled"] is False
    assert client.post("/revoke", json={"darwin_id": "d"}).status_code == 404


def test_revoke_requires_darwin_id():
    client = _client(revocations=RevocationRegistry())
    assert client.post("/revoke", json={}).status_code == 400


# ---------------------------------------------------------------------------
# serve entrypoint exposes bandwidth-quota flags
# ---------------------------------------------------------------------------

def test_build_parser_accepts_bandwidth_flags():
    args = build_parser().parse_args([
        "serve", "--allowlist", "/tmp/a.json",
        "--bandwidth-soft-gb", "10", "--bandwidth-hard-gb", "20",
    ])
    assert args.bandwidth_soft_gb == 10.0
    assert args.bandwidth_hard_gb == 20.0


# ---------------------------------------------------------------------------
# Supervisor.kill → egress cut
# ---------------------------------------------------------------------------

class _RecordingRevoker:
    def __init__(self):
        self.revoked: list[str] = []

    def revoke(self, darwin_id: str) -> None:
        self.revoked.append(darwin_id)


@pytest.mark.asyncio
async def test_supervisor_kill_cuts_egress():
    from darwin_platform.runtime.supervisor import Supervisor

    revoker = _RecordingRevoker()
    sup = Supervisor(heartbeat_seconds=0.001, egress_revoker=revoker)
    status = await sup.spawn(opening_usdc=100.0, actor="test")
    did = status.darwin_id
    try:
        await sup.kill(did, reason="owner kill", actor="op")
        assert did in revoker.revoked
        assert len(sup.audit_log.entries(action="egress_revoked")) == 1
    finally:
        await sup.shutdown(kill_running=True)


# -- HttpEgressRevoker.bandwidth_used_today (resource-anomaly source) -------
class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_bandwidth_used_today_reads_per_darwin_usage(monkeypatch):
    from darwin_platform.infra.egress_proxy import HttpEgressRevoker

    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return _FakeResp({"enabled": True,
                          "per_darwin_usage": {"d1": 12345}})

    monkeypatch.setattr("httpx.get", fake_get)
    rev = HttpEgressRevoker(base_url="http://proxy:9000")
    assert rev.bandwidth_used_today("d1") == 12345.0
    assert calls[0] == "http://proxy:9000/bandwidth"
    # A Darwin the proxy knows nothing about → 0.0 (not None).
    assert rev.bandwidth_used_today("unknown") == 0.0


def test_bandwidth_used_today_none_when_disabled_or_unreachable(monkeypatch):
    from darwin_platform.infra.egress_proxy import HttpEgressRevoker

    rev = HttpEgressRevoker(base_url="http://proxy:9000")
    monkeypatch.setattr("httpx.get", lambda url, timeout=None: _FakeResp({"enabled": False}))
    assert rev.bandwidth_used_today("d1") is None

    def boom(url, timeout=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("httpx.get", boom)
    assert rev.bandwidth_used_today("d1") is None
