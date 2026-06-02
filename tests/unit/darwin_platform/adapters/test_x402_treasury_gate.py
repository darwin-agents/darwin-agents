"""x402 settlement runs through the treasury policy gate (one money door, doc 05).

Previously x402 auto-settlement had only a flat client-side cap; the full policy
stack (per-action / per-day / lifetime caps + escalation) was bypassed. These
tests verify the injected gate is consulted before the wallet signs, and that
build_treasury_gate refuses a spend the policy engine won't ALLOW.
"""

from __future__ import annotations

import pytest

from darwin_platform.adapters.x402 import (
    X402Error,
    X402PaymentClient,
    build_treasury_gate,
)
from darwin_fakes.fake_wallet import FakeWallet


class _Resp:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class _HTTP:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0)


def _wallet(opening=100.0):
    return FakeWallet(opening_usdc=opening, clock=lambda: 0.0)


def _payment_required(amount="0.50"):
    return _Resp(402, headers={
        "X-Payment-Required-Amount-Usdc": amount,
        "X-Payment-Required-Address": "0x" + "ab" * 20,
        "X-Payment-Required-Nonce": "n1",
    })


# ---------------------------------------------------------------------------
# the gate hook
# ---------------------------------------------------------------------------

def test_gate_refusal_blocks_settlement_before_wallet_signs():
    w = _wallet()

    def _deny(amount, counterparty):
        raise ValueError("policy refused")

    client = X402PaymentClient(http=_HTTP([_payment_required()]), wallet=w,
                               treasury_gate=_deny)
    with pytest.raises(X402Error, match="treasury policy refused"):
        client.request("GET", "https://api/paywall")
    # The wallet never moved money.
    assert w.balance_usdc() == 100.0
    assert w.spent_lifetime_usdc() == 0.0


def test_gate_allow_lets_settlement_proceed():
    w = _wallet()
    seen: list = []

    def _allow(amount, counterparty):
        seen.append((amount, counterparty))

    client = X402PaymentClient(
        http=_HTTP([_payment_required("0.50"), _Resp(200)]),
        wallet=w, treasury_gate=_allow,
    )
    resp = client.request("GET", "https://api/paywall")
    assert resp.status_code == 200
    assert seen == [(0.50, "0x" + "ab" * 20)]
    assert w.spent_lifetime_usdc() == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# build_treasury_gate against real policy
# ---------------------------------------------------------------------------

def test_build_treasury_gate_allows_small_spend():
    gate = build_treasury_gate(_wallet())
    gate(0.50, "0x" + "cd" * 20)  # under every cap → no raise


def test_build_treasury_gate_refuses_over_per_action_review():
    # A spend over the per-action escalation-review threshold is not ALLOW;
    # the gate raises so x402 aborts rather than silently signing.
    gate = build_treasury_gate(_wallet(opening=10_000.0))
    with pytest.raises(X402Error):
        gate(5_000.0, "0x" + "cd" * 20)


def test_x402_default_has_no_gate_backwards_compatible():
    # Without a gate the flat cap still applies (legacy behaviour preserved).
    client = X402PaymentClient(
        http=_HTTP([_payment_required("99.0")]), wallet=_wallet(),
        max_payment_usdc=5.0,
    )
    with pytest.raises(X402Error, match="client cap"):
        client.request("GET", "https://api/paywall")
