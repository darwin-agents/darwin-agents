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
