"""Stripe fiat-ramp service (docs/engineering/22 §"Fiat on-ramp (Stripe)").

A thin orchestrator over :mod:`darwin_platform.adapters.fiat`'s
:class:`StripeFiatRamp` (and :class:`StubFiatRamp` for tests). The
service:

1. Tracks every deposit attempt the platform observes (idempotent on
   ``provider_tx_id``) — Stripe webhooks can replay; we must not
   double-credit.
2. On ``payment_intent.succeeded`` (the webhook), records the deposit
   on :class:`OmnibusWallet` and the operator audit log.
3. Reconciles Stripe charges vs. the omnibus balance daily; surfaces
   any discrepancy as a refusal (Phase-2 §"Billing reconciliation").

KYC integration: a deposit attempt by a non-APPROVED subject is
refused at the service layer before any Stripe call.

Webhook signature verification is delegated to the Stripe SDK; this
module accepts an already-verified event payload (a dict) so the API
route can verify with the SDK and pass us the parsed envelope.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .adapters.fiat import FiatRamp, FiatRampError
from .kyc import KYCError, KYCService
from .omnibus_wallet import OmnibusError, OmnibusWallet


class StripeRampError(Exception):
    """The service refused the deposit / withdrawal."""


@dataclass(frozen=True)
class DepositRecord:
    """One observed deposit. Keyed by ``provider_tx_id`` so webhook
    replays are idempotent."""

    provider_tx_id: str
    user_subject: str
    amount_usd: float
    amount_usdc: float
    fee_usd: float
    status: str               # "pending" | "succeeded" | "failed"
    destination_darwin_id: str | None
    quote_id: str
    created_at: float
    updated_at: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "provider_tx_id": self.provider_tx_id,
            "user_subject": self.user_subject,
            "amount_usd": self.amount_usd,
            "amount_usdc": self.amount_usdc,
            "fee_usd": self.fee_usd,
            "status": self.status,
            "destination_darwin_id": self.destination_darwin_id,
            "quote_id": self.quote_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }


class StripeRampService:
    """High-level deposit/withdraw flow on top of :class:`FiatRamp`.

    Construction:

    ::

        svc = StripeRampService(
            ramp=StripeFiatRamp(stripe_client=stripe),
            omnibus=omnibus,
            kyc=kyc_service,        # optional but recommended
        )

    The service NEVER touches the ramp directly — every call goes
    through ``self._ramp`` so the StubFiatRamp can stand in for tests.
    """

    def __init__(
        self,
        *,
        ramp: FiatRamp,
        omnibus: OmnibusWallet,
        kyc: KYCService | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ramp = ramp
        self._omnibus = omnibus
        self._kyc = kyc
        self._clock = clock
        # Deposits we've observed (the webhook replay guard).
        self._deposits: dict[str, DepositRecord] = {}
        # Quote → owning user subject + intended darwin (set on quote;
        # consumed on confirm). Lets the webhook resolve the user from
        # the quote id Stripe carries in metadata.
        self._quote_owner: dict[str, tuple[str, str | None]] = {}
        # Audit recorder bound by the supervisor (subject, action,
        # metadata). Same shape as the KYC service uses.
        self._audit_recorder: (
            Callable[[str, str, dict[str, Any]], None] | None
        ) = None

    def bind_audit_recorder(
        self,
        recorder: Callable[[str, str, dict[str, Any]], None],
    ) -> None:
        self._audit_recorder = recorder

    # -- quote (KYC-gated; no on-chain effect) -----------------------------
    def create_deposit_quote(
        self,
        *,
        user_subject: str,
        amount_usd: float,
        payment_method: str,
        destination_darwin_id: str | None = None,
        actor: str = "platform.api",
    ) -> dict[str, Any]:
        """KYC-gated. Returns the quote envelope the dashboard hands
        Stripe Checkout. The quote id is also recorded so the eventual
        webhook can resolve the user without trusting Stripe's metadata
        alone."""
        if self._kyc is not None:
            try:
                self._kyc.require_allowed(user_subject, action="deposit")
            except KYCError as exc:
                self._audit("stripe_deposit_refused", actor, {
                    "user_subject": user_subject,
                    "reason": f"kyc:{exc}",
                })
                raise StripeRampError(str(exc)) from exc
        try:
            quote = self._ramp.deposit_quote(
                amount_usd=amount_usd, payment_method=payment_method,
            )
        except FiatRampError as exc:
            raise StripeRampError(f"ramp quote failed: {exc}") from exc
        self._quote_owner[quote.quote_id] = (user_subject, destination_darwin_id)
        self._audit("stripe_quote_created", actor, {
            "user_subject": user_subject,
            "quote_id": quote.quote_id,
            "amount_usd": float(amount_usd),
            "amount_usdc": float(quote.amount_usdc),
        })
        return {
            "quote_id": quote.quote_id,
            "amount_usd": quote.amount_usd,
            "amount_usdc": quote.amount_usdc,
            "fee_usd": quote.fee_usd,
            "rate": quote.rate,
            "ttl_seconds": quote.ttl_seconds,
            "provider": quote.provider,
        }

    # -- webhook (succeeded path) ------------------------------------------
    def handle_webhook(
        self,
        event: dict[str, Any],
        *,
        actor: str = "platform.stripe_webhook",
    ) -> DepositRecord:
        """Consume a verified Stripe event envelope. Returns the record
        the platform now holds for the deposit. Idempotent: a
        replayed webhook for the same ``provider_tx_id`` is a no-op."""
        event_type = str(event.get("type", ""))
        data = (event.get("data", {}) or {}).get("object", {}) or {}
        if event_type != "payment_intent.succeeded":
            raise StripeRampError(
                f"unsupported event type {event_type!r}"
            )
        provider_tx_id = str(data.get("id") or "")
        if not provider_tx_id:
            raise StripeRampError("event has no payment_intent id")
        if provider_tx_id in self._deposits:
            return self._deposits[provider_tx_id]
        metadata = data.get("metadata") or {}
        quote_id = str(metadata.get("quote_id") or "")
        if not quote_id:
            raise StripeRampError(
                "payment_intent missing quote_id in metadata"
            )
        owner = self._quote_owner.pop(quote_id, None)
        if owner is None:
            raise StripeRampError(
                f"unknown quote_id {quote_id!r} — no owner recorded"
            )
        user_subject, dst_darwin = owner

        try:
            receipt = self._ramp.confirm_deposit(
                quote_id=quote_id, destination_darwin_id=dst_darwin,
            )
        except FiatRampError as exc:
            raise StripeRampError(f"ramp confirm failed: {exc}") from exc

        try:
            self._omnibus.record_deposit(
                user_subject=user_subject,
                amount_usdc=receipt.amount_usdc,
                source_ref=f"stripe:{provider_tx_id}",
                actor=actor,
            )
        except OmnibusError as exc:
            raise StripeRampError(f"omnibus record_deposit failed: {exc}") from exc

        record = DepositRecord(
            provider_tx_id=provider_tx_id,
            user_subject=user_subject,
            amount_usd=receipt.amount_usd,
            amount_usdc=receipt.amount_usdc,
            fee_usd=receipt.fee_usd,
            status="succeeded",
            destination_darwin_id=dst_darwin,
            quote_id=quote_id,
            created_at=self._clock(),
            updated_at=self._clock(),
            metadata={"event_type": event_type},
        )
        self._deposits[provider_tx_id] = record
        self._audit("stripe_deposit_credited", actor, {
            "provider_tx_id": provider_tx_id,
            "user_subject": user_subject,
            "amount_usdc": record.amount_usdc,
        })
        return record

    # -- withdraw -----------------------------------------------------------
    def withdraw(
        self,
        *,
        user_subject: str,
        amount_usdc: float,
        destination: str,
        actor: str = "platform.api",
    ) -> dict[str, Any]:
        """KYC-gated user withdrawal. Pulls from the user's omnibus
        credit balance and pushes through the Stripe Connect payout."""
        if self._kyc is not None:
            try:
                self._kyc.require_allowed(user_subject, action="withdraw")
            except KYCError as exc:
                raise StripeRampError(str(exc)) from exc

        try:
            self._omnibus.withdraw_to_user(
                user_subject=user_subject,
                amount_usdc=amount_usdc,
                to_address=destination,
                actor=actor,
            )
        except OmnibusError as exc:
            raise StripeRampError(f"omnibus refused withdraw: {exc}") from exc

        try:
            wd = self._ramp.withdraw(
                amount_usdc=amount_usdc, destination=destination,
            )
        except FiatRampError as exc:
            # The omnibus has already debited; the operator must
            # reconcile. Surface a clear error so the audit reflects it.
            self._audit("stripe_withdraw_ramp_failed", actor, {
                "user_subject": user_subject,
                "amount_usdc": float(amount_usdc),
                "reason": str(exc),
            })
            raise StripeRampError(f"ramp withdraw failed: {exc}") from exc

        self._audit("stripe_withdraw_completed", actor, {
            "user_subject": user_subject,
            "amount_usdc": float(amount_usdc),
            "destination": destination,
            "provider_tx_id": wd.provider_tx_id,
        })
        return {
            "provider_tx_id": wd.provider_tx_id,
            "amount_usdc": wd.amount_usdc,
            "amount_usd": wd.amount_usd,
            "fee_usd": wd.fee_usd,
            "destination": wd.destination,
            "provider": wd.provider,
        }

    # -- read-side ----------------------------------------------------------
    def list_deposits(self, *, user_subject: str | None = None) -> list[DepositRecord]:
        if user_subject is None:
            return list(self._deposits.values())
        return [
            r for r in self._deposits.values()
            if r.user_subject == user_subject
        ]

    def get_deposit(self, provider_tx_id: str) -> DepositRecord | None:
        return self._deposits.get(provider_tx_id)

    # -- internals ---------------------------------------------------------
    def _audit(self, action: str, actor: str, metadata: dict[str, Any]) -> None:
        if self._audit_recorder is None:
            return
        try:
            self._audit_recorder(actor, action, metadata)
        except Exception:
            pass
