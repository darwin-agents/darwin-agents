"""Typed Python client for the Darwin Platform HTTP API.

Mirrors the typed surface in ``web/lib/api.ts`` for programmatic
consumers — CI scripts, integration tests, ad-hoc operator automation,
etc. The genome itself never uses this; it's the platform-side counterpart
to the React frontend.

Scope is intentionally focused on the payments surface added in docs/05
+ docs/17 (preapprovals, freelancer registry, fiat payouts, refund,
omnibus retry). Existing endpoints (spawn / kill / events / vault) are
left to either grow into this client over time or stay curl-able from
the runbook — adding everything now would force a much larger surface
review.

Usage::

    from darwin_platform.client import DarwinClient, FreelancerRegistration

    client = DarwinClient(base_url="https://platform.example.com",
                          token="op_xyz")

    client.register_freelancer(FreelancerRegistration(
        owner_subject="alice", freelancer_id="upwork:writer-42",
        fiat_destination="acct_1Q...", kyc_status="verified",
    ))
    payout = client.darwin_fiat_payment(
        darwin_id="d-abc", amount_usdc=25.0,
        freelancer_id="upwork:writer-42",
        escalation_id="esc-9001",
    )

Every mutating call sends an ``Idempotency-Key`` header derived from
``secrets.token_hex`` so an accidental retry of a POST doesn't
double-settle. The frontend does the same; the contract is the
supervisor's :class:`OmnibusEntry` table's natural-key uniqueness.

Errors surface as :class:`PlatformApiError` which carries the HTTP
status + the server's ``detail`` string so callers can branch on
``isinstance(exc, PlatformApiError) and exc.status == 409`` (the
"freelancer not registered" / "omnibus not configured" shape) vs 404
(unknown darwin) without parsing prose.
"""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx


class PlatformApiError(Exception):
    """Non-2xx response from the platform. The status code is preserved
    so callers can branch on it without prose parsing."""

    def __init__(
        self, status: int, code: str, detail: Any, *, message: str | None = None,
    ) -> None:
        super().__init__(message or f"HTTP {status}: {code}")
        self.status = status
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Request bodies (typed dataclasses that round-trip to JSON)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreApprovalSpec:
    """Body for ``PUT /platform/wallets/{id}/preapprovals/{rule_id}``."""

    max_amount_usdc: float
    counterparty_category: str | None = None
    counterparty_substring: str | None = None
    expires_at: float | None = None
    max_uses: int | None = None

    def to_body(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class FreelancerRegistration:
    """Body for ``POST /platform/freelancers`` (operator) or
    ``POST /me/freelancers`` (owner self-serve, ``owner_subject`` is
    overridden server-side)."""

    owner_subject: str
    freelancer_id: str
    fiat_destination: str
    display_name: str = ""
    kyc_status: str = "unverified"
    metadata: dict[str, Any] | None = field(default=None)

    def to_body(self) -> dict[str, Any]:
        body = asdict(self)
        if body["metadata"] is None:
            body.pop("metadata")
        return body


@dataclass(frozen=True)
class FiatPaymentRequest:
    """Body for ``POST /darwins/{id}/fiat_payment``."""

    amount_usdc: float
    freelancer_id: str
    escalation_id: str | None = None

    def to_body(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class FiatRefundRequest:
    """Body for ``POST /darwins/{id}/fiat_payment/refund``."""

    amount_usdc: float
    reason: str = "fiat_payout_leg2_failed"

    def to_body(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class DarwinClient:
    """Synchronous typed wrapper around the payments-related HTTP routes.

    Pass either an existing ``httpx.Client`` (for tests / shared connection
    pools) or a ``base_url`` and ``token`` to construct one. Caller owns
    closing the client when ``transport`` was injected; otherwise
    :meth:`close` (or use as a context manager) closes the internal one."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        client_header: str = "darwin-python-client/0.1",
    ) -> None:
        if client is None and base_url is None:
            raise ValueError("DarwinClient requires base_url or client")
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            headers: dict[str, str] = {"X-Darwin-Client": client_header}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            self._client = httpx.Client(
                base_url=base_url or "",
                timeout=timeout,
                headers=headers,
                transport=transport,
            )
            self._owns_client = True

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> DarwinClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- request helper ----------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        mutating: bool = False,
    ) -> Any:
        headers: dict[str, str] | None = None
        if mutating:
            headers = {"Idempotency-Key": secrets.token_hex(8)}
        resp = self._client.request(method, path, json=json, headers=headers)
        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except Exception:
                payload = resp.text
            detail = (
                payload.get("detail")
                if isinstance(payload, dict)
                else str(payload)
            )
            raise PlatformApiError(
                status=resp.status_code,
                code=str(detail or resp.reason_phrase),
                detail=payload,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # -- pre-approvals -----------------------------------------------------
    def list_preapprovals(self, darwin_id: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/platform/wallets/{darwin_id}/preapprovals",
        )

    def set_preapproval(
        self, darwin_id: str, rule_id: str, spec: PreApprovalSpec,
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/platform/wallets/{darwin_id}/preapprovals/{rule_id}",
            json=spec.to_body(),
            mutating=True,
        )

    def revoke_preapproval(
        self, darwin_id: str, rule_id: str,
    ) -> dict[str, Any]:
        return self._request(
            "DELETE",
            f"/platform/wallets/{darwin_id}/preapprovals/{rule_id}",
            mutating=True,
        )

    # -- freelancer registry (operator) -----------------------------------
    def register_freelancer(
        self, registration: FreelancerRegistration,
    ) -> dict[str, Any]:
        return self._request(
            "POST", "/platform/freelancers",
            json=registration.to_body(),
            mutating=True,
        )

    def list_freelancers(self, owner_subject: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/platform/freelancers/{owner_subject}",
        )

    def revoke_freelancer(
        self, owner_subject: str, freelancer_id: str,
    ) -> dict[str, Any]:
        return self._request(
            "DELETE",
            f"/platform/freelancers/{owner_subject}/{freelancer_id}",
            mutating=True,
        )

    # -- freelancer registry (/me self-serve) ------------------------------
    def register_my_freelancer(
        self, registration: FreelancerRegistration,
    ) -> dict[str, Any]:
        """``owner_subject`` in the body is ignored server-side; the
        authenticated subject is used instead."""
        return self._request(
            "POST", "/me/freelancers",
            json=registration.to_body(),
            mutating=True,
        )

    def list_my_freelancers(self) -> list[dict[str, Any]]:
        return self._request("GET", "/me/freelancers")

    def revoke_my_freelancer(self, freelancer_id: str) -> dict[str, Any]:
        return self._request(
            "DELETE", f"/me/freelancers/{freelancer_id}",
            mutating=True,
        )

    # -- darwin fiat payment + refund + omnibus retry ---------------------
    def darwin_fiat_payment(
        self, darwin_id: str, request: FiatPaymentRequest,
    ) -> dict[str, Any]:
        return self._request(
            "POST", f"/darwins/{darwin_id}/fiat_payment",
            json=request.to_body(),
            mutating=True,
        )

    def darwin_fiat_refund(
        self, darwin_id: str, request: FiatRefundRequest,
    ) -> dict[str, Any]:
        return self._request(
            "POST", f"/darwins/{darwin_id}/fiat_payment/refund",
            json=request.to_body(),
            mutating=True,
        )

    def retry_omnibus_fund(
        self, darwin_id: str, *, amount_usdc: float,
    ) -> dict[str, Any]:
        return self._request(
            "POST", f"/darwins/{darwin_id}/omnibus/fund",
            json={"amount_usdc": float(amount_usdc)},
            mutating=True,
        )


__all__ = [
    "DarwinClient",
    "FiatPaymentRequest",
    "FiatRefundRequest",
    "FreelancerRegistration",
    "PlatformApiError",
    "PreApprovalSpec",
]
