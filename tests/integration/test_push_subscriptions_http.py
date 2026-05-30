"""HTTP-layer tests for the Web Push subscription endpoints (doc 22).

Exercises the four owner-scoped routes end-to-end through the FastAPI
ASGI transport:

- GET    /me/push-vapid-public-key
- POST   /me/push-subscriptions
- GET    /me/push-subscriptions
- DELETE /me/push-subscriptions

The supervisor methods themselves + persistence are covered by
``test_notification_webpush.py`` and ``test_push_subscription_store.py``;
this module focuses on the wiring between the request body, the
FastAPI route handler, the supervisor call, and the auth gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from darwin_platform.api import build_app  # noqa: E402
from darwin_platform.escalations.notification_webpush import (  # noqa: E402
    WebPushNotificationSink,
)
from darwin_platform.runtime.supervisor import Supervisor  # noqa: E402


class _StubTransport:
    def send(self, **kw):
        return {"status_code": 201, "body": "", "error": None}


@pytest.fixture
async def push_client(tmp_path: Path):
    """Supervisor wired with a persistent store + a Web Push sink so the
    full register/list/revoke path is reachable."""
    sink = WebPushNotificationSink(
        vapid_private_key="-----BEGIN EC PRIVATE KEY-----\nstub\n-----END EC PRIVATE KEY-----",
        vapid_subject="mailto:ops@example.com",
        transport=_StubTransport(),
        darwin_owner_resolver=lambda _did: "anonymous",
    )
    sup = Supervisor(
        heartbeat_seconds=0.001,
        persistence_path=str(tmp_path / "p.db"),
        notification_sink=sink,
        push_vapid_public_key="BSomeBase64UrlPublicKey",
    )
    app = build_app(sup)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            yield c, sup, sink
        finally:
            await sup.shutdown(kill_running=True)


async def test_vapid_public_key_returns_configured_value(push_client):
    c, _sup, _sink = push_client
    r = await c.get("/me/push-vapid-public-key")
    assert r.status_code == 200
    assert r.json() == {"public_key": "BSomeBase64UrlPublicKey"}


async def test_vapid_public_key_503_when_not_configured(tmp_path: Path):
    # Supervisor with no public key wired → endpoint refuses.
    sup = Supervisor(
        heartbeat_seconds=0.001,
        persistence_path=str(tmp_path / "p.db"),
    )
    app = build_app(sup)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            r = await c.get("/me/push-vapid-public-key")
            assert r.status_code == 503
        finally:
            await sup.shutdown(kill_running=True)


async def test_register_list_revoke_round_trip(push_client):
    c, _sup, sink = push_client

    endpoint = "https://fcm.googleapis.com/wp/A"
    r = await c.post("/me/push-subscriptions", json={
        "endpoint": endpoint,
        "p256dh": "p1",
        "auth": "a1",
        "user_agent": "Chrome/130",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["registered"] is True
    # Endpoint is redacted to origin in the response.
    assert body["endpoint_origin"] == "https://fcm.googleapis.com"
    assert "endpoint" not in body  # never returned in full

    # List returns the subscription with the endpoint redacted to origin.
    r = await c.get("/me/push-subscriptions")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["endpoint_origin"] == "https://fcm.googleapis.com"
    assert rows[0]["user_agent"] == "Chrome/130"
    assert "endpoint" not in rows[0]
    assert "p256dh" not in rows[0]
    assert "auth" not in rows[0]

    # The live sink was also told about the subscription.
    assert len(sink.subscriptions_for("anonymous")) == 1

    # Revoke by endpoint (DELETE with ?endpoint=… — proxies strip DELETE
    # bodies often enough that we use a query param here).
    r = await c.delete(
        "/me/push-subscriptions",
        params={"endpoint": endpoint},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"revoked": True}

    # Subsequent list is empty + sink is empty.
    assert (await c.get("/me/push-subscriptions")).json() == []
    assert sink.subscriptions_for("anonymous") == []


async def test_register_validates_required_fields(push_client):
    c, _sup, _sink = push_client
    # FastAPI rejects empty required strings at the pydantic layer (422).
    r = await c.post("/me/push-subscriptions", json={
        "endpoint": "", "p256dh": "p", "auth": "a",
    })
    assert r.status_code == 422


async def test_register_persists_across_restart(tmp_path: Path):
    db_path = str(tmp_path / "p.db")

    # First supervisor populates the DB.
    sink1 = WebPushNotificationSink(
        vapid_private_key="-----BEGIN EC PRIVATE KEY-----\nstub\n-----END EC PRIVATE KEY-----",
        vapid_subject="mailto:ops@example.com",
        transport=_StubTransport(),
        darwin_owner_resolver=lambda _did: "anonymous",
    )
    sup1 = Supervisor(
        heartbeat_seconds=0.001,
        persistence_path=db_path,
        notification_sink=sink1,
        push_vapid_public_key="BSomeBase64UrlPublicKey",
    )
    app1 = build_app(sup1)
    transport1 = httpx.ASGITransport(app=app1)
    async with httpx.AsyncClient(
        transport=transport1, base_url="http://t",
    ) as c1:
        await c1.post("/me/push-subscriptions", json={
            "endpoint": "https://fcm.googleapis.com/wp/A",
            "p256dh": "p", "auth": "a",
        })
    await sup1.shutdown(kill_running=True)

    # Reopen — the new supervisor's persistent store rehydrates the new sink.
    sink2 = WebPushNotificationSink(
        vapid_private_key="-----BEGIN EC PRIVATE KEY-----\nstub\n-----END EC PRIVATE KEY-----",
        vapid_subject="mailto:ops@example.com",
        transport=_StubTransport(),
        darwin_owner_resolver=lambda _did: "anonymous",
    )
    sup2 = Supervisor(
        heartbeat_seconds=0.001,
        persistence_path=db_path,
        notification_sink=sink2,
        push_vapid_public_key="BSomeBase64UrlPublicKey",
    )
    app2 = build_app(sup2)
    transport2 = httpx.ASGITransport(app=app2)
    try:
        async with httpx.AsyncClient(
            transport=transport2, base_url="http://t",
        ) as c2:
            rows = (await c2.get("/me/push-subscriptions")).json()
            assert len(rows) == 1
            assert rows[0]["endpoint_origin"] == "https://fcm.googleapis.com"
        # The sink was also auto-hydrated from the persistent store.
        assert len(sink2.subscriptions_for("anonymous")) == 1
    finally:
        await sup2.shutdown(kill_running=True)
