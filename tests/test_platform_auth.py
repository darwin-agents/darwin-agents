"""Auth coverage for the FastAPI surface.

Two scopes: ``owner`` (per-Darwin actions, restricted to the token's
owned set) and ``platform_operator`` (whole-fleet + every owner action).
Auth is opt-in: ``build_app(supervisor)`` keeps the trust-everyone mode
existing tests rely on; ``build_app(supervisor, token_store=...)`` enables
bearer auth.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from darwin_platform import (  # noqa: E402
    InMemoryTokenStore,
    OWNER,
    PLATFORM_OPERATOR,
    Supervisor,
)
from darwin_platform.api import build_app  # noqa: E402


@pytest.fixture
async def signed_client():
    """Supervisor + auth-enabled app + pre-issued owner / operator tokens."""
    store = InMemoryTokenStore()
    store.issue("owner-tok", scopes={OWNER}, subject="alice@example.com")
    store.issue("other-owner-tok", scopes={OWNER}, subject="bob@example.com")
    store.issue("ops-tok", scopes={PLATFORM_OPERATOR}, subject="ops@dwn")

    sup = Supervisor(heartbeat_seconds=0.0)
    app = build_app(sup, token_store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            yield c, sup, store
        finally:
            await sup.shutdown()


def _auth(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


# -- missing / bad tokens -------------------------------------------------
async def test_no_token_returns_401(signed_client) -> None:
    c, _, _ = signed_client
    r = await c.post("/darwins", json={"opening_usdc": 60.0})
    assert r.status_code == 401
    assert "Bearer" in r.headers.get("www-authenticate", "")


async def test_invalid_token_returns_401(signed_client) -> None:
    c, _, _ = signed_client
    r = await c.post(
        "/darwins", json={"opening_usdc": 60.0}, headers=_auth("nope")
    )
    assert r.status_code == 401


async def test_malformed_header_returns_401(signed_client) -> None:
    c, _, _ = signed_client
    r = await c.post(
        "/darwins",
        json={"opening_usdc": 60.0},
        headers={"Authorization": "Token owner-tok"},  # not Bearer
    )
    assert r.status_code == 401


# -- ownership ------------------------------------------------------------
async def test_spawn_grants_ownership_to_caller(signed_client) -> None:
    c, _, store = signed_client
    r = await c.post(
        "/darwins", json={"opening_usdc": 60.0}, headers=_auth("owner-tok")
    )
    assert r.status_code == 201
    did = r.json()["darwin_id"]
    # The token store now records this id under the caller's owned set.
    ctx = store.lookup("owner-tok")
    assert did in ctx.owned_darwins


async def test_owner_cannot_act_on_anothers_darwin(signed_client) -> None:
    c, _, _ = signed_client
    r = await c.post(
        "/darwins", json={"opening_usdc": 60.0}, headers=_auth("owner-tok")
    )
    did = r.json()["darwin_id"]

    # alice's Darwin is invisible to bob.
    r = await c.get(f"/darwins/{did}", headers=_auth("other-owner-tok"))
    assert r.status_code == 403
    r = await c.post(
        f"/darwins/{did}/kill",
        json={"reason": "noscope"},
        headers=_auth("other-owner-tok"),
    )
    assert r.status_code == 403


async def test_owner_list_returns_only_their_own(signed_client) -> None:
    c, _, _ = signed_client
    r = await c.post(
        "/darwins", json={"opening_usdc": 60.0}, headers=_auth("owner-tok")
    )
    alice_id = r.json()["darwin_id"]
    r = await c.post(
        "/darwins",
        json={"opening_usdc": 40.0},
        headers=_auth("other-owner-tok"),
    )
    bob_id = r.json()["darwin_id"]

    r = await c.get("/darwins", headers=_auth("owner-tok"))
    listed = r.json()["darwins"]
    assert alice_id in listed
    assert bob_id not in listed


# -- platform_operator scope ----------------------------------------------
async def test_operator_can_act_on_any_darwin(signed_client) -> None:
    c, _, _ = signed_client
    r = await c.post(
        "/darwins", json={"opening_usdc": 60.0}, headers=_auth("owner-tok")
    )
    did = r.json()["darwin_id"]

    # Operator can read someone else's Darwin.
    r = await c.get(f"/darwins/{did}", headers=_auth("ops-tok"))
    assert r.status_code == 200

    # Operator can quarantine — owner cannot.
    r = await c.post(
        f"/platform/darwins/{did}/quarantine",
        json={"reason": "review"},
        headers=_auth("owner-tok"),
    )
    assert r.status_code == 403
    r = await c.post(
        f"/platform/darwins/{did}/quarantine",
        json={"reason": "review"},
        headers=_auth("ops-tok"),
    )
    assert r.status_code == 200


async def test_owner_cannot_read_fleet_snapshot(signed_client) -> None:
    c, _, _ = signed_client
    r = await c.get("/platform/snapshot", headers=_auth("owner-tok"))
    assert r.status_code == 403
    r = await c.get("/platform/snapshot", headers=_auth("ops-tok"))
    assert r.status_code == 200


async def test_owner_cannot_read_fleet_alerts_but_their_own_yes(
    signed_client,
) -> None:
    c, _, _ = signed_client
    r = await c.post(
        "/darwins", json={"opening_usdc": 60.0}, headers=_auth("owner-tok")
    )
    did = r.json()["darwin_id"]

    # Per-darwin alerts: owner OK.
    r = await c.get(f"/darwins/{did}/alerts", headers=_auth("owner-tok"))
    assert r.status_code == 200

    # Fleet alerts: owner refused.
    r = await c.get("/platform/alerts", headers=_auth("owner-tok"))
    assert r.status_code == 403

    # Fleet alerts: operator OK.
    r = await c.get("/platform/alerts", headers=_auth("ops-tok"))
    assert r.status_code == 200


async def test_signing_public_key_requires_operator(signed_client) -> None:
    c, _, _ = signed_client
    r = await c.get("/platform/signing/public_key", headers=_auth("owner-tok"))
    assert r.status_code == 403
    r = await c.get("/platform/signing/public_key", headers=_auth("ops-tok"))
    assert r.status_code == 200


# -- spawn-grant flow + revoke --------------------------------------------
async def test_revoke_blocks_further_access(signed_client) -> None:
    c, _, store = signed_client
    r = await c.post(
        "/darwins", json={"opening_usdc": 60.0}, headers=_auth("owner-tok")
    )
    did = r.json()["darwin_id"]
    # Confirm baseline access.
    r = await c.get(f"/darwins/{did}", headers=_auth("owner-tok"))
    assert r.status_code == 200

    store.revoke("owner-tok")
    r = await c.get(f"/darwins/{did}", headers=_auth("owner-tok"))
    assert r.status_code == 401  # token no longer recognised
