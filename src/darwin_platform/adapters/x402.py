"""x402 HTTP-payment adapter (docs/engineering/17 §"The payment infrastructure").

x402 is Coinbase's HTTP-native payment protocol: a service returns
``HTTP 402 Payment Required`` with a price + recipient in the response
headers, the agent's wallet authorizes a USDC payment, and the agent
retries with payment proof. This adapter is the platform-side glue
that turns a 402 into a wallet ``pay`` + retry — no per-neuron
plumbing.

The wider doc-17 stack (Coinbase Agentic Wallets + x402 + Stripe
Privy) is mostly external; what we own here is the *interception*
side: any neuron that wants pay-as-you-go API access wires this
client and stops caring about 402 handling.

Protocol shape (Coinbase x402 v2, Dec 2025):

- Server response on 402 carries these headers we read:
    - ``X-Payment-Required-Address``: recipient (USDC ERC-20 address)
    - ``X-Payment-Required-Amount-Usdc``: float
    - ``X-Payment-Required-Nonce``: opaque server-issued nonce
- Client retries with:
    - ``X-Payment-Tx``: tx hash (or x402 receipt) returned by the
      wallet
    - ``X-Payment-Required-Nonce``: echoed back unchanged
- ``X-Payment-Max-Usdc``: server-side cap echo (cosmetic; we cap
  client-side via ``max_payment_usdc``).

The header names are NOT standardised across all x402 deployments
(different services use slightly different prefixes); the constructor
accepts overrides for each.

Out of scope for this MVP:
- Reusable sessions (x402 V2 added these for batched billing).
- Multi-chain routing (USDC on Solana / Stellar in addition to Base).
- Receipt verification on the server side (the platform's job, not
  the client's).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class X402Error(Exception):
    """Raised when 402 handling fails irrecoverably — bad headers,
    over-cap price, payment refused by the wallet, retry budget
    exhausted."""


@runtime_checkable
class HTTPClient(Protocol):
    """The slice of an HTTP client we need. Matches ``httpx.Client.request``
    in shape so the obvious wiring works; tests inject a stub."""

    def request(
        self, method: str, url: str, **kwargs: Any
    ) -> Any: ...


class X402PaymentClient:
    """Wraps an HTTP client + a wallet so callers can issue ``request()``
    without worrying about 402.

    The wrapper transparently:
        1. Sends the original request.
        2. If the response is 402, reads the required-amount + recipient
           + nonce from the headers (header names configurable).
        3. Calls ``wallet.pay(to=..., amount_usdc=..., memo=...)``.
           Wallet refusal (policy, hard cap, insufficient funds)
           surfaces as :class:`X402Error`.
        4. Re-issues the request with the payment proof headers.
        5. Bounded retry: by default ONE payment attempt per
           ``request`` so a misconfigured server can't loop the wallet
           dry. Configurable.

    Args:
        http: any object with a ``request(method, url, **kwargs)`` method
            that returns a response with ``status_code`` + ``headers`` +
            ``json()``-like API (or our own minimal envelope from tests).
        wallet: :class:`WalletPort` from the genome (or the platform
            adapters' :class:`RealWallet`).
        max_payment_usdc: per-call ceiling. Any required-amount above
            this is refused before the wallet sees it. Defaults to $5
            — a sane cap for a single API call.
        max_retries: number of 402-retry round trips per request. 1 is
            the safe default; raise only when you trust the upstream.
    """

    # Default header names (case-insensitive). Override per-server.
    HDR_AMOUNT = "X-Payment-Required-Amount-Usdc"
    HDR_ADDRESS = "X-Payment-Required-Address"
    HDR_NONCE = "X-Payment-Required-Nonce"
    HDR_TX = "X-Payment-Tx"

    def __init__(
        self,
        *,
        http: HTTPClient,
        wallet: Any,
        max_payment_usdc: float = 5.0,
        max_retries: int = 1,
        header_names: dict[str, str] | None = None,
        treasury_gate: Any | None = None,
    ) -> None:
        self._http = http
        self._wallet = wallet
        self._max = float(max_payment_usdc)
        self._max_retries = max(0, int(max_retries))
        # Treasury policy gate (docs/engineering/05 §"one money door"). When
        # set, every x402 settlement is run through the full policy stack
        # (per-action / per-day / per-lifetime caps, soft caps, escalation)
        # BEFORE the wallet signs — not just the flat client-side cap. The
        # callable is ``gate(amount_usdc: float, counterparty: str) -> None``
        # and RAISES when the spend is refused or requires escalation. The
        # client-side ``max_payment_usdc`` stays as a hard backstop.
        self._treasury_gate = treasury_gate
        names = {
            "amount": self.HDR_AMOUNT,
            "address": self.HDR_ADDRESS,
            "nonce": self.HDR_NONCE,
            "tx": self.HDR_TX,
        }
        if header_names:
            names.update(header_names)
        self._h = names

    # -- public -----------------------------------------------------------
    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Mirror of ``http.request`` with transparent 402 handling. Returns
        the upstream's response object once the call succeeds; raises
        :class:`X402Error` on payment failure or retry exhaustion."""
        attempts = 0
        while True:
            resp = self._http.request(method, url, **kwargs)
            if _status(resp) != 402:
                return resp
            if attempts >= self._max_retries:
                raise X402Error(
                    f"x402 retry budget exhausted at {url} after "
                    f"{attempts} attempt(s)"
                )
            attempts += 1
            self._pay_for(resp, url, kwargs)

    # -- internals --------------------------------------------------------
    def _pay_for(self, resp: Any, url: str, kwargs: dict) -> None:
        headers = _headers(resp)
        amount_raw = headers.get(self._h["amount"]) or headers.get(
            self._h["amount"].lower()
        )
        address = headers.get(self._h["address"]) or headers.get(
            self._h["address"].lower()
        )
        nonce = headers.get(self._h["nonce"]) or headers.get(
            self._h["nonce"].lower()
        )
        if amount_raw is None or address is None:
            raise X402Error(
                f"x402 response at {url} is missing required headers "
                f"(amount={amount_raw!r}, address={address!r})"
            )
        try:
            amount = float(amount_raw)
        except (TypeError, ValueError) as exc:
            raise X402Error(
                f"x402 amount header {amount_raw!r} is not a number"
            ) from exc
        if amount <= 0:
            raise X402Error(
                f"x402 amount must be positive; got {amount}"
            )
        if amount > self._max:
            raise X402Error(
                f"x402 required amount ${amount:.4f} > client cap "
                f"${self._max:.4f}"
            )

        # One money door (doc 05): run the spend through the treasury policy
        # engine before the wallet signs, so per-action / per-day / lifetime
        # caps + escalation apply to x402 settlement just like a direct
        # payment — not merely the flat client-side cap above.
        if self._treasury_gate is not None:
            try:
                self._treasury_gate(amount, address)
            except X402Error:
                raise
            except Exception as exc:
                raise X402Error(
                    f"x402 treasury policy refused ${amount:.4f} to "
                    f"{address}: {exc}"
                ) from exc

        try:
            tx = self._wallet.pay(
                to=address, amount_usdc=amount,
                memo=f"x402:{url}",
            )
        except Exception as exc:
            raise X402Error(f"x402 wallet refused: {exc}") from exc
        tx_hash = (
            tx.get("tx_hash") if isinstance(tx, dict) else getattr(tx, "tx_hash", None)
        )
        if not tx_hash:
            raise X402Error("x402 wallet did not return a tx_hash")

        # Inject the payment headers into the retry. We do not mutate
        # the caller's headers dict — copy first.
        merged_headers = dict(kwargs.get("headers") or {})
        merged_headers[self._h["tx"]] = str(tx_hash)
        if nonce is not None:
            merged_headers[self._h["nonce"]] = str(nonce)
        kwargs["headers"] = merged_headers


def build_treasury_gate(wallet: Any, *, category: str = "ops.api"):
    """Build a treasury-policy gate for :class:`X402PaymentClient`.

    The returned ``gate(amount_usdc, counterparty)`` evaluates each x402
    settlement through the SAME policy engine ``payments.facilitator`` uses
    (per-action / per-day / per-lifetime caps, owner soft caps, pre-approvals),
    reading live state from ``wallet``, and RAISES :class:`X402Error` on a
    REFUSE/ESCALATE verdict so the settlement aborts. This restores the
    "one money door" invariant (doc 05) for pay-as-you-go HTTP spend, which
    previously only had a flat client-side cap. Optional wallet capabilities
    (soft caps, pre-approvals) degrade gracefully when absent.
    """

    def _read(name: str, default: Any) -> Any:
        fn = getattr(wallet, name, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                return default
        return default

    def _gate(amount_usdc: float, counterparty: str) -> None:
        from darwin_dna.constitution import SpendIntent
        from darwin_dna.machinery.treasury_policy import (
            TreasuryState,
            Verdict,
            evaluate,
        )

        state = TreasuryState(
            balance_usdc=float(_read("balance_usdc", 0.0)),
            spent_today_usdc=float(_read("spent_today_usdc", 0.0)),
            spent_lifetime_usdc=float(_read("spent_lifetime_usdc", 0.0)),
            recent_spend_usdc=tuple(_read("recent_spend_usdc", ()) or ()),
            soft_caps=_read("owner_soft_caps", None),
            pre_approvals=tuple(_read("owner_preapprovals", ()) or ()),
        )
        intent = SpendIntent(
            amount_usdc=float(amount_usdc),
            counterparty=counterparty,
            counterparty_category="unknown",
            category=category,
        )
        decision = evaluate(intent, state)
        if decision.verdict is not Verdict.ALLOW:
            raise X402Error(
                f"treasury policy {decision.verdict.value}: {decision.reason}"
            )

    return _gate


# -- response shape helpers (tolerant of httpx + stubs) -------------------
def _status(resp: Any) -> int:
    return int(
        getattr(resp, "status_code", None)
        or (resp.get("status_code") if isinstance(resp, dict) else 0)
        or 0
    )


def _headers(resp: Any) -> dict[str, str]:
    h = getattr(resp, "headers", None)
    if h is None and isinstance(resp, dict):
        h = resp.get("headers") or {}
    return dict(h or {})
