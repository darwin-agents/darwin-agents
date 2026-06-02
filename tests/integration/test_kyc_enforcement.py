"""KYC + ToS-consent + 18+ enforcement on the live fund/spawn path (docs 05/22).

Before this, KYC was dead code (require_allowed only reachable from an orphaned
service) and there was no consent/age capture anywhere. These tests pin:
- the age gate (require_adult) in KYCService,
- spawn + fiat gated on an APPROVED, adult subject when KYC is configured,
- the /platform/kyc/* HTTP surface,
- spawn-consent enforcement when the app requires it.
"""

from __future__ import annotations

import pytest

from darwin_platform.security.kyc import (
    KYCError,
    KYCService,
    KYCStatus,
    KYCTier,
    KYCVerdict,
    StubKYCProvider,
)
from darwin_platform.runtime.supervisor import Supervisor, SupervisorError


def _approved_service(subject: str, *, is_adult: bool = True) -> KYCService:
    provider = StubKYCProvider()
    if not is_adult:
        provider.set_override(subject, KYCVerdict(
            subject=subject, status=KYCStatus.APPROVED, is_adult=False,
        ))
    svc = KYCService(provider=provider)
    out = svc.start_inquiry(subject=subject)
    svc.refresh_status(subject=subject, inquiry_id=out["inquiry_id"])
    return svc


# ---------------------------------------------------------------------------
# KYCService age gate
# ---------------------------------------------------------------------------

def test_require_adult_refuses_minor():
    svc = _approved_service("kid", is_adult=False)
    # Plain approval is fine...
    assert svc.require_allowed("kid").status == KYCStatus.APPROVED
    # ...but the money/spawn path requires 18+.
    with pytest.raises(KYCError, match="18"):
        svc.require_allowed("kid", action="spawn", require_adult=True)


def test_require_adult_allows_adult():
    svc = _approved_service("grownup", is_adult=True)
    assert svc.require_allowed(
        "grownup", action="spawn", require_adult=True,
    ).status == KYCStatus.APPROVED


def test_require_allowed_refuses_unknown_subject():
    svc = KYCService(provider=StubKYCProvider())
    with pytest.raises(KYCError):
        svc.require_allowed("never-verified", action="fund")


# ---------------------------------------------------------------------------
# Supervisor spawn + fiat gating
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spawn_refused_for_unverified_owner():
    sup = Supervisor(
        heartbeat_seconds=0.001,
        kyc_service=KYCService(provider=StubKYCProvider()),
    )
    try:
        with pytest.raises(SupervisorError, match="KYC"):
            await sup.spawn(opening_usdc=50.0, actor="stranger")
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_spawn_allowed_for_verified_adult_owner():
    sup = Supervisor(
        heartbeat_seconds=0.001,
        kyc_service=_approved_service("owner"),
    )
    try:
        status = await sup.spawn(opening_usdc=50.0, actor="owner")
        assert status.darwin_id
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_spawn_unaffected_without_kyc_service():
    # No KYC configured (dev / tests) → gate is a no-op.
    sup = Supervisor(heartbeat_seconds=0.001)
    try:
        status = await sup.spawn(opening_usdc=50.0, actor="anyone")
        assert status.darwin_id
    finally:
        await sup.shutdown(kill_running=True)


# ---------------------------------------------------------------------------
# HTTP surface + consent enforcement
# ---------------------------------------------------------------------------

def _client(sup, **build_kwargs):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    return TestClient(build_app(sup, **build_kwargs))


def test_kyc_http_start_status_webhook_roundtrip():
    sup = Supervisor(
        heartbeat_seconds=0.001,
        kyc_service=KYCService(provider=StubKYCProvider()),
    )
    with _client(sup) as client:
        # Start an inquiry.
        r = client.post("/platform/kyc/start", json={"subject": "alice"})
        assert r.status_code == 200
        inq = r.json()["inquiry_id"]
        # Before refresh, status is pending/unknown — not approved.
        r2 = client.get("/platform/kyc/status", params={"subject": "alice"})
        assert r2.json()["status"] in ("unknown", "pending")
        # Webhook/poll consumption flips it to approved (stub default).
        r3 = client.post(
            "/platform/kyc/webhook",
            json={"subject": "alice", "inquiry_id": inq},
        )
        assert r3.json()["status"] == "approved"
        assert r3.json()["is_adult"] is True


def test_kyc_routes_503_when_unconfigured():
    sup = Supervisor(heartbeat_seconds=0.001)  # no kyc_service
    with _client(sup) as client:
        assert client.post(
            "/platform/kyc/start", json={"subject": "x"}
        ).status_code == 503


def test_spawn_requires_consent_when_enabled():
    sup = Supervisor(heartbeat_seconds=0.001)
    with _client(sup, require_spawn_consent=True) as client:
        # Missing consent → 400.
        r = client.post("/darwins", json={"opening_usdc": 50.0})
        assert r.status_code == 400
        # With consent → spawns.
        r2 = client.post("/darwins", json={
            "opening_usdc": 50.0,
            "terms_accepted": True,
            "consent_18_plus": True,
        })
        assert r2.status_code == 201


def test_spawn_consent_optional_by_default():
    sup = Supervisor(heartbeat_seconds=0.001)
    with _client(sup) as client:  # require_spawn_consent defaults False
        r = client.post("/darwins", json={"opening_usdc": 50.0})
        assert r.status_code == 201
