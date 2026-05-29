"""HTTP-layer tests for the payments-related endpoints (doc 17).

Exercises every new route end-to-end through the FastAPI ASGI transport:

- Pre-approvals CRUD (PUT / DELETE / GET /platform/wallets/.../preapprovals)
- Freelancer registry CRUD (POST / DELETE / GET /platform/freelancers/...)
- Darwin → fiat payout (POST /darwins/{id}/fiat_payment)
- Fiat-payout refund (POST /darwins/{id}/fiat_payment/refund)

The supervisor methods themselves are covered by
``test_phase2_supervisor.py``; this module focuses on the wiring between
the request body, the FastAPI route handler, and the supervisor call —
status codes, body shapes, and the auth gate.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from darwin_platform.adapters.fiat import StubFiatRamp  # noqa: E402
from darwin_platform.api import build_app  # noqa: E402
from darwin_platform.payments.freelancer_registry import (  # noqa: E402
    InMemoryFreelancerRegistry,
)
from darwin_platform.payments.omnibus_wallet import OmnibusWallet  # noqa: E402
from darwin_platform.runtime.supervisor import Supervisor  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class _OmnibusChain:
    """Tiny in-process chain stub for the omnibus wallet."""

    def __init__(self, balance: float = 10_000.0) -> None:
        self.balance = balance
        self.transfers: list[dict] = []
        self.fail_next = False

    def address(self) -> str:
        return "0xomnibus"

    def balance_usdc(self) -> float:
        return self.balance

    def transfer(self, *, to, amount_usdc, memo):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("rpc down")
        self.balance -= amount_usdc
        h = f"0xtx{len(self.transfers):08x}"
        self.transfers.append({"to": to, "amount_usdc": amount_usdc,
                               "memo": memo, "tx_hash": h})
        return {"tx_hash": h}


@pytest.fixture
async def basic_client():
    """Plain supervisor — no omnibus / ramp / registry."""
    sup = Supervisor(heartbeat_seconds=0.001)
    app = build_app(sup)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            yield c, sup
        finally:
            await sup.shutdown(kill_running=True)


@pytest.fixture
async def fiat_client():
    """Supervisor wired with omnibus + ramp + freelancer registry — the
    full Darwin → fiat path is reachable."""
    sup = Supervisor(
        heartbeat_seconds=0.001,
        omnibus_wallet=OmnibusWallet(_OmnibusChain()),
        fiat_ramp=StubFiatRamp(),
        freelancer_registry=InMemoryFreelancerRegistry(),
    )
    app = build_app(sup)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            yield c, sup
        finally:
            await sup.shutdown(kill_running=True)


async def _spawn(c, **kw) -> str:
    r = await c.post("/darwins", json={"opening_usdc": 100.0, **kw})
    assert r.status_code == 201, r.text
    return r.json()["darwin_id"]


# ---------------------------------------------------------------------------
# Pre-approvals CRUD
# ---------------------------------------------------------------------------


async def test_preapproval_put_get_delete_round_trip(basic_client):
    c, _ = basic_client
    did = await _spawn(c)

    # Initially empty.
    r = await c.get(f"/platform/wallets/{did}/preapprovals")
    assert r.status_code == 200
    assert r.json() == []

    # Install one.
    body = {
        "max_amount_usdc": 50.0,
        "counterparty_category": "freelancer",
        "counterparty_substring": "upwork",
        "max_uses": 3,
    }
    r = await c.put(
        f"/platform/wallets/{did}/preapprovals/upwork-3",
        json=body,
    )
    assert r.status_code == 200, r.text
    stored = r.json()
    assert stored["rule_id"] == "upwork-3"
    assert stored["max_amount_usdc"] == 50.0
    assert stored["max_uses"] == 3
    assert stored["used_count"] == 0

    # List shows it.
    r = await c.get(f"/platform/wallets/{did}/preapprovals")
    listed = r.json()
    assert [d["rule_id"] for d in listed] == ["upwork-3"]

    # Delete.
    r = await c.delete(f"/platform/wallets/{did}/preapprovals/upwork-3")
    assert r.status_code == 200
    assert r.json() == {"revoked": True, "rule_id": "upwork-3"}

    # Idempotent delete.
    r = await c.delete(f"/platform/wallets/{did}/preapprovals/upwork-3")
    assert r.status_code == 200
    assert r.json() == {"revoked": False, "rule_id": "upwork-3"}


async def test_preapproval_put_validation(basic_client):
    c, _ = basic_client
    did = await _spawn(c)

    # Missing max_amount_usdc → 422.
    r = await c.put(
        f"/platform/wallets/{did}/preapprovals/r1",
        json={"counterparty_category": "freelancer"},
    )
    assert r.status_code == 422

    # max_amount_usdc must be > 0.
    r = await c.put(
        f"/platform/wallets/{did}/preapprovals/r1",
        json={"max_amount_usdc": 0.0},
    )
    assert r.status_code == 422

    # max_uses must be >= 1 when given.
    r = await c.put(
        f"/platform/wallets/{did}/preapprovals/r1",
        json={"max_amount_usdc": 10.0, "max_uses": 0},
    )
    assert r.status_code == 422


async def test_preapproval_unknown_darwin_404(basic_client):
    c, _ = basic_client
    r = await c.put(
        "/platform/wallets/does-not-exist/preapprovals/r1",
        json={"max_amount_usdc": 10.0},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Freelancer registry CRUD
# ---------------------------------------------------------------------------


async def test_freelancer_post_list_delete_round_trip(fiat_client):
    c, _ = fiat_client
    body = {
        "owner_subject": "alice",
        "freelancer_id": "upwork:writer-42",
        "fiat_destination": "acct_freelancer_bank_1",
        "display_name": "Jane Writer",
        "kyc_status": "verified",
    }
    r = await c.post("/platform/freelancers", json=body)
    assert r.status_code == 200, r.text
    stored = r.json()
    assert stored["owner_subject"] == "alice"
    assert stored["freelancer_id"] == "upwork:writer-42"
    assert stored["kyc_status"] == "verified"

    # List for alice.
    r = await c.get("/platform/freelancers/alice")
    assert r.status_code == 200
    listed = r.json()
    assert [d["freelancer_id"] for d in listed] == ["upwork:writer-42"]

    # Bob has nothing.
    r = await c.get("/platform/freelancers/bob")
    assert r.json() == []

    # Revoke.
    r = await c.delete("/platform/freelancers/alice/upwork:writer-42")
    assert r.status_code == 200
    assert r.json() == {"revoked": True, "freelancer_id": "upwork:writer-42"}

    # Idempotent revoke.
    r = await c.delete("/platform/freelancers/alice/upwork:writer-42")
    assert r.json()["revoked"] is False


async def test_freelancer_post_replaces_existing(fiat_client):
    """Registering twice under the same (owner, id) updates rather than
    appending — same row, new destination."""
    c, _ = fiat_client
    body_v1 = {
        "owner_subject": "alice",
        "freelancer_id": "f1",
        "fiat_destination": "acct_v1",
    }
    body_v2 = {**body_v1, "fiat_destination": "acct_v2", "kyc_status": "verified"}
    await c.post("/platform/freelancers", json=body_v1)
    await c.post("/platform/freelancers", json=body_v2)
    listed = (await c.get("/platform/freelancers/alice")).json()
    assert len(listed) == 1
    assert listed[0]["fiat_destination"] == "acct_v2"
    assert listed[0]["kyc_status"] == "verified"


async def test_freelancer_post_validation(fiat_client):
    c, _ = fiat_client

    # Missing fiat_destination → 422.
    r = await c.post("/platform/freelancers", json={
        "owner_subject": "alice", "freelancer_id": "f1",
    })
    assert r.status_code == 422

    # Empty owner_subject → 422 (min_length=1).
    r = await c.post("/platform/freelancers", json={
        "owner_subject": "", "freelancer_id": "f1",
        "fiat_destination": "acct_x",
    })
    assert r.status_code == 422


async def test_freelancer_post_without_registry_returns_409(basic_client):
    """Supervisor constructed without a registry refuses the call."""
    c, _ = basic_client
    r = await c.post("/platform/freelancers", json={
        "owner_subject": "alice", "freelancer_id": "f1",
        "fiat_destination": "acct_x",
    })
    assert r.status_code == 409
    assert "registry" in r.text.lower()


# ---------------------------------------------------------------------------
# darwin → fiat payout
# ---------------------------------------------------------------------------


async def test_fiat_payment_happy_path(fiat_client):
    c, sup = fiat_client
    # Auth-disabled mode makes the spawn actor "anonymous"; the freelancer
    # must be registered under that owner_subject for the lookup to match.
    did = await _spawn(c)

    # Register the freelancer first.
    await c.post("/platform/freelancers", json={
        "owner_subject": "anonymous",
        "freelancer_id": "upwork:x",
        "fiat_destination": "acct_x",
        "kyc_status": "verified",
    })
    # Fire the payout.
    r = await c.post(f"/darwins/{did}/fiat_payment", json={
        "amount_usdc": 25.0,
        "freelancer_id": "upwork:x",
        "escalation_id": "esc-99",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "darwin_fiat_payout"
    assert body["counterparty"] == did
    # Audit + omnibus ledger reflect the call (verified end-to-end in
    # test_phase2_supervisor; here we just assert the route returned ok).
    audits = sup.audit_log.entries(action="darwin_fiat_payout")
    assert len(audits) == 1
    assert audits[0].metadata["escalation_id"] == "esc-99"


async def test_fiat_payment_unregistered_freelancer_409(fiat_client):
    c, _ = fiat_client
    did = await _spawn(c, actor="alice")
    # No freelancer registered → refused before any chain transfer.
    r = await c.post(f"/darwins/{did}/fiat_payment", json={
        "amount_usdc": 25.0,
        "freelancer_id": "ghost",
    })
    assert r.status_code == 409
    assert "not registered" in r.text


async def test_fiat_payment_requires_omnibus_409(basic_client):
    """With no omnibus / ramp / registry wired, the route refuses with
    the supervisor's "not configured" error."""
    c, _ = basic_client
    did = await _spawn(c, actor="alice")
    r = await c.post(f"/darwins/{did}/fiat_payment", json={
        "amount_usdc": 10.0,
        "freelancer_id": "x",
    })
    assert r.status_code == 409


async def test_fiat_payment_validation(fiat_client):
    c, _ = fiat_client
    did = await _spawn(c, actor="alice")

    # amount_usdc must be > 0.
    r = await c.post(f"/darwins/{did}/fiat_payment", json={
        "amount_usdc": 0.0, "freelancer_id": "x",
    })
    assert r.status_code == 422

    # freelancer_id required.
    r = await c.post(f"/darwins/{did}/fiat_payment", json={
        "amount_usdc": 1.0,
    })
    assert r.status_code == 422


async def test_fiat_payment_refund_happy_path(fiat_client):
    c, sup = fiat_client
    did = await _spawn(c)
    await c.post("/platform/freelancers", json={
        "owner_subject": "anonymous",
        "freelancer_id": "f1",
        "fiat_destination": "acct_x",
    })
    await c.post(f"/darwins/{did}/fiat_payment", json={
        "amount_usdc": 25.0, "freelancer_id": "f1",
    })

    # Refund the payout (operator intervention on a hypothetical ramp failure).
    r = await c.post(f"/darwins/{did}/fiat_payment/refund", json={
        "amount_usdc": 25.0,
        "reason": "ramp_outage",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "refund_to_darwin"
    # Audit captures the reason.
    audits = sup.audit_log.entries(action="refund_fiat_payout")
    assert len(audits) == 1
    assert audits[0].metadata["reason"] == "ramp_outage"


async def test_fiat_payment_refund_validation(fiat_client):
    c, _ = fiat_client
    did = await _spawn(c, actor="alice")
    # amount_usdc > 0 required.
    r = await c.post(f"/darwins/{did}/fiat_payment/refund", json={
        "amount_usdc": 0.0,
    })
    assert r.status_code == 422


async def test_fiat_payment_refund_unknown_darwin_404(fiat_client):
    c, _ = fiat_client
    r = await c.post("/darwins/does-not-exist/fiat_payment/refund", json={
        "amount_usdc": 1.0,
    })
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Owner self-serve /me/freelancers
# ---------------------------------------------------------------------------


async def test_me_freelancers_round_trip(fiat_client):
    """Auth-disabled mode → ctx.subject = 'anonymous'. The /me routes
    implicitly scope to that subject. Registering, listing, deleting
    from /me lands in the same store the /platform routes use."""
    c, _ = fiat_client
    # Owner registers via /me — no owner_subject in the body needs to match.
    r = await c.post("/me/freelancers", json={
        "owner_subject": "ignored-by-route",   # overridden to ctx.subject
        "freelancer_id": "f-self-serve",
        "fiat_destination": "acct_self",
        "kyc_status": "verified",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["owner_subject"] == "anonymous"   # NOT "ignored-by-route"
    assert body["freelancer_id"] == "f-self-serve"

    # List via /me returns just this owner's entries.
    listed = (await c.get("/me/freelancers")).json()
    assert [d["freelancer_id"] for d in listed] == ["f-self-serve"]

    # Revoke via /me.
    r = await c.delete("/me/freelancers/f-self-serve")
    assert r.status_code == 200
    assert r.json() == {"revoked": True, "freelancer_id": "f-self-serve"}
    assert (await c.get("/me/freelancers")).json() == []


async def test_me_freelancers_cannot_reach_another_owner(fiat_client):
    """The body's owner_subject is overridden by ctx.subject — an owner
    cannot register under someone else's identity by spoofing the body."""
    c, sup = fiat_client
    # Alice (via the operator routes) seeds an entry.
    await c.post("/platform/freelancers", json={
        "owner_subject": "alice",
        "freelancer_id": "alice-only",
        "fiat_destination": "acct_alice",
    })
    # The /me caller (ctx.subject="anonymous") tries to spoof alice in body.
    await c.post("/me/freelancers", json={
        "owner_subject": "alice",
        "freelancer_id": "imposter",
        "fiat_destination": "acct_evil",
    })
    # /me list only shows the imposter under "anonymous" — alice's row
    # is untouched.
    me_listed = (await c.get("/me/freelancers")).json()
    assert [d["freelancer_id"] for d in me_listed] == ["imposter"]
    # Alice's list still has just alice-only.
    alice_listed = (await c.get("/platform/freelancers/alice")).json()
    assert [d["freelancer_id"] for d in alice_listed] == ["alice-only"]


async def test_me_freelancers_delete_unknown_is_idempotent(fiat_client):
    c, _ = fiat_client
    r = await c.delete("/me/freelancers/never-existed")
    assert r.status_code == 200
    assert r.json()["revoked"] is False
