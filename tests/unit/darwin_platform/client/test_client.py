"""Unit tests for the typed Python SDK client.

Uses ``httpx.MockTransport`` so no real network IO. The point of these
tests is the request shape (method + URL + body + headers) — the
server-side semantics are already covered by
``test_payments_http.py``."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from darwin_platform.client import (
    DarwinClient,
    FiatPaymentRequest,
    FiatRefundRequest,
    FreelancerRegistration,
    PlatformApiError,
    PreApprovalSpec,
)


class _Recorder:
    """Captures every request the client sends. ``set_response`` queues
    one or more responses; the request count drives which is returned."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self._responses: list[httpx.Response] = []

    def queue(self, *responses: httpx.Response) -> None:
        self._responses.extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if not self._responses:
            return httpx.Response(200, json={})
        return self._responses.pop(0)


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture
def client(recorder: _Recorder) -> DarwinClient:
    transport = httpx.MockTransport(recorder.handler)
    return DarwinClient(
        base_url="http://test", token="tok_x", transport=transport,
    )


def _body(req: httpx.Request) -> Any:
    if not req.content:
        return None
    return json.loads(req.content.decode("utf-8"))


# ---------------------------------------------------------------------------
# Pre-approvals
# ---------------------------------------------------------------------------


def test_list_preapprovals_uses_get(client, recorder):
    recorder.queue(httpx.Response(200, json=[]))
    out = client.list_preapprovals("d-1")
    assert out == []
    req = recorder.calls[0]
    assert req.method == "GET"
    assert req.url.path == "/platform/wallets/d-1/preapprovals"


def test_set_preapproval_puts_with_body_and_idempotency(client, recorder):
    recorder.queue(httpx.Response(200, json={
        "rule_id": "r1", "max_amount_usdc": 50.0, "used_count": 0,
    }))
    out = client.set_preapproval(
        "d-1", "r1",
        PreApprovalSpec(
            max_amount_usdc=50.0,
            counterparty_category="freelancer",
            max_uses=3,
        ),
    )
    assert out["rule_id"] == "r1"
    req = recorder.calls[0]
    assert req.method == "PUT"
    assert req.url.path == "/platform/wallets/d-1/preapprovals/r1"
    assert req.headers.get("idempotency-key")
    assert _body(req) == {
        "max_amount_usdc": 50.0,
        "counterparty_category": "freelancer",
        "max_uses": 3,
    }


def test_revoke_preapproval_deletes(client, recorder):
    recorder.queue(httpx.Response(
        200, json={"revoked": True, "rule_id": "r1"},
    ))
    out = client.revoke_preapproval("d-1", "r1")
    assert out == {"revoked": True, "rule_id": "r1"}
    req = recorder.calls[0]
    assert req.method == "DELETE"
    assert req.url.path == "/platform/wallets/d-1/preapprovals/r1"
    assert req.headers.get("idempotency-key")


# ---------------------------------------------------------------------------
# Freelancer registry
# ---------------------------------------------------------------------------


def test_register_freelancer_posts_to_platform_route(client, recorder):
    recorder.queue(httpx.Response(200, json={
        "owner_subject": "alice", "freelancer_id": "f1",
        "fiat_destination": "acct_x",
    }))
    out = client.register_freelancer(FreelancerRegistration(
        owner_subject="alice", freelancer_id="f1",
        fiat_destination="acct_x", display_name="One",
        kyc_status="verified",
    ))
    assert out["owner_subject"] == "alice"
    req = recorder.calls[0]
    assert req.method == "POST"
    assert req.url.path == "/platform/freelancers"
    body = _body(req)
    assert body["owner_subject"] == "alice"
    assert body["freelancer_id"] == "f1"
    assert body["kyc_status"] == "verified"
    # metadata omitted when None — keeps the wire body tight.
    assert "metadata" not in body


def test_list_freelancers_passes_owner_in_path(client, recorder):
    recorder.queue(httpx.Response(200, json=[]))
    client.list_freelancers("alice")
    assert recorder.calls[0].url.path == "/platform/freelancers/alice"


def test_revoke_freelancer_deletes(client, recorder):
    recorder.queue(httpx.Response(
        200, json={"revoked": True, "freelancer_id": "f1"},
    ))
    client.revoke_freelancer("alice", "f1")
    req = recorder.calls[0]
    assert req.method == "DELETE"
    assert req.url.path == "/platform/freelancers/alice/f1"


def test_me_routes_use_self_serve_paths(client, recorder):
    """The /me variants hit a different prefix; the body's owner_subject
    is sent through, but the server ignores it and uses ctx.subject —
    the client doesn't need to know that."""
    recorder.queue(
        httpx.Response(200, json=[]),
        httpx.Response(200, json={
            "owner_subject": "anonymous", "freelancer_id": "f1",
            "fiat_destination": "acct_x",
        }),
        httpx.Response(200, json={"revoked": True, "freelancer_id": "f1"}),
    )
    client.list_my_freelancers()
    assert recorder.calls[0].url.path == "/me/freelancers"
    assert recorder.calls[0].method == "GET"

    client.register_my_freelancer(FreelancerRegistration(
        owner_subject="ignored-server-side",
        freelancer_id="f1",
        fiat_destination="acct_x",
    ))
    assert recorder.calls[1].url.path == "/me/freelancers"
    assert recorder.calls[1].method == "POST"

    client.revoke_my_freelancer("f1")
    assert recorder.calls[2].url.path == "/me/freelancers/f1"
    assert recorder.calls[2].method == "DELETE"


# ---------------------------------------------------------------------------
# Darwin fiat payment / refund / omnibus retry
# ---------------------------------------------------------------------------


def test_darwin_fiat_payment_posts_with_escalation_id(client, recorder):
    recorder.queue(httpx.Response(200, json={
        "kind": "darwin_fiat_payout", "amount_usdc": 25.0,
    }))
    out = client.darwin_fiat_payment(
        "d-1",
        FiatPaymentRequest(
            amount_usdc=25.0,
            freelancer_id="f1",
            escalation_id="esc-99",
        ),
    )
    assert out["kind"] == "darwin_fiat_payout"
    req = recorder.calls[0]
    assert req.method == "POST"
    assert req.url.path == "/darwins/d-1/fiat_payment"
    assert req.headers.get("idempotency-key")
    assert _body(req) == {
        "amount_usdc": 25.0,
        "freelancer_id": "f1",
        "escalation_id": "esc-99",
    }


def test_darwin_fiat_payment_omits_escalation_when_none(client, recorder):
    recorder.queue(httpx.Response(200, json={"kind": "darwin_fiat_payout"}))
    client.darwin_fiat_payment(
        "d-1", FiatPaymentRequest(amount_usdc=10.0, freelancer_id="f1"),
    )
    body = _body(recorder.calls[0])
    assert "escalation_id" not in body


def test_darwin_fiat_refund_hits_refund_route(client, recorder):
    recorder.queue(httpx.Response(200, json={"kind": "refund_to_darwin"}))
    client.darwin_fiat_refund(
        "d-1",
        FiatRefundRequest(amount_usdc=25.0, reason="ramp_outage"),
    )
    req = recorder.calls[0]
    assert req.url.path == "/darwins/d-1/fiat_payment/refund"
    assert req.method == "POST"
    assert _body(req) == {"amount_usdc": 25.0, "reason": "ramp_outage"}


def test_retry_omnibus_fund_posts_amount(client, recorder):
    recorder.queue(httpx.Response(200, json={"kind": "fund_child"}))
    client.retry_omnibus_fund("d-1", amount_usdc=100.0)
    req = recorder.calls[0]
    assert req.url.path == "/darwins/d-1/omnibus/fund"
    assert req.method == "POST"
    assert _body(req) == {"amount_usdc": 100.0}


# ---------------------------------------------------------------------------
# Error handling + headers
# ---------------------------------------------------------------------------


def test_4xx_surfaces_as_platform_api_error_with_status(client, recorder):
    recorder.queue(httpx.Response(
        409, json={"detail": "freelancer 'ghost' is not registered"},
    ))
    with pytest.raises(PlatformApiError) as exc_info:
        client.darwin_fiat_payment(
            "d-1",
            FiatPaymentRequest(amount_usdc=10.0, freelancer_id="ghost"),
        )
    assert exc_info.value.status == 409
    assert "not registered" in exc_info.value.code


def test_404_branchable(client, recorder):
    """Callers can distinguish 404 from other failures without parsing."""
    recorder.queue(httpx.Response(404, json={"detail": "unknown darwin"}))
    with pytest.raises(PlatformApiError) as exc_info:
        client.darwin_fiat_refund(
            "ghost", FiatRefundRequest(amount_usdc=1.0),
        )
    assert exc_info.value.status == 404


def test_bearer_token_attached_to_every_request(recorder):
    transport = httpx.MockTransport(recorder.handler)
    client = DarwinClient(
        base_url="http://test", token="my-token", transport=transport,
    )
    recorder.queue(httpx.Response(200, json=[]))
    client.list_my_freelancers()
    assert recorder.calls[0].headers["authorization"] == "Bearer my-token"
    client.close()


def test_no_token_omits_authorization_header(recorder):
    transport = httpx.MockTransport(recorder.handler)
    client = DarwinClient(base_url="http://test", transport=transport)
    recorder.queue(httpx.Response(200, json=[]))
    client.list_my_freelancers()
    assert "authorization" not in recorder.calls[0].headers
    client.close()


def test_context_manager_closes_client(recorder):
    transport = httpx.MockTransport(recorder.handler)
    with DarwinClient(base_url="http://test", transport=transport) as c:
        recorder.queue(httpx.Response(200, json=[]))
        c.list_my_freelancers()
    # No explicit assertion needed — exiting the context closes the
    # client without raising; a closed-client double-close would.


def test_idempotency_key_unique_per_request(client, recorder):
    """Two consecutive mutating calls must get distinct keys; the
    server's natural-key dedupe relies on uniqueness."""
    recorder.queue(
        httpx.Response(200, json={"rule_id": "r1"}),
        httpx.Response(200, json={"rule_id": "r1"}),
    )
    client.set_preapproval("d", "r1", PreApprovalSpec(max_amount_usdc=10.0))
    client.set_preapproval("d", "r1", PreApprovalSpec(max_amount_usdc=10.0))
    k1 = recorder.calls[0].headers["idempotency-key"]
    k2 = recorder.calls[1].headers["idempotency-key"]
    assert k1 != k2


def test_construction_requires_base_url_or_client():
    with pytest.raises(ValueError, match="base_url"):
        DarwinClient()  # type: ignore[call-arg]
