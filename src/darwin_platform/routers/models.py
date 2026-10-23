"""Pydantic request bodies for the Darwin Supervisor HTTP surface.

Extracted from ``darwin_platform.api``; re-exported there for backward
compatibility.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SpawnRequest(BaseModel):
    opening_usdc: float = Field(default=100.0, gt=0)
    escalation_auto: str | None = Field(
        default=None,
        description="Unattended owner-response policy: 'approve' | 'deny' | "
        "null. Default null: the owner must respond via the "
        "respond_escalation endpoint. 'approve' auto-approves every ticket "
        "(spend, captcha, KYC) and is meant for unattended dev/sim runs.",
    )
    parent_id: str | None = None
    # Owner consent (doc 01 step 3 + doc 05). Captured + audit-recorded on
    # every spawn; ENFORCED (400 without both) when the app is built with
    # ``require_spawn_consent=True`` (production). Defaults False so dev /
    # in-process tests are unaffected.
    terms_accepted: bool = Field(
        default=False,
        description="Owner accepted the kill-switch terms of service.",
    )
    consent_18_plus: bool = Field(
        default=False,
        description="Owner attests they are 18 or older.",
    )


class KillRequest(BaseModel):
    reason: str = "owner kill"


class PauseRequest(BaseModel):
    reason: str = "owner paused"


class EscalationResponseRequest(BaseModel):
    decision: str = Field(pattern=r"^(approve|deny|data)$")
    data: dict[str, Any] | None = None


class QuarantineRequest(BaseModel):
    reason: str
    operator: str = "platform"


class ResourceThrottleRequest(BaseModel):
    kind: str = Field(pattern=r"^(sandbox|browser|egress)$")
    max_daily_calls: int = Field(ge=0)


class FiatDepositQuoteRequest(BaseModel):
    amount_usd: float = Field(gt=0)
    payment_method: str = Field(min_length=1)
    destination_darwin_id: str | None = None


class FiatConfirmDepositRequest(BaseModel):
    quote_id: str = Field(min_length=1)
    destination_darwin_id: str | None = None


class FleetSpendLockRequest(BaseModel):
    locked: bool = True


class FleetBlockCategoryRequest(BaseModel):
    category: str = Field(min_length=1)


class BlockCounterpartyRequest(BaseModel):
    address: str = Field(min_length=1)


class KYCStartRequest(BaseModel):
    # The user to verify. Defaults to the authenticated caller; operators may
    # start an inquiry on behalf of another subject.
    subject: str | None = None
    tier: str = Field(default="standard", pattern=r"^(basic|standard|enhanced)$")


class KYCWebhookRequest(BaseModel):
    # Default: the caller. Only operators may name another subject.
    subject: str | None = None
    inquiry_id: str = Field(min_length=1)


class DarwinFiatPayoutRequest(BaseModel):
    """Owner-approved fiat payout from a Darwin's wallet to a verified
    fiat destination (doc 17 §"Darwin paying a human"). ``freelancer_id``
    must resolve through the platform's freelancer registry for the
    Darwin's owning user. ``escalation_id`` ties the payout back to the
    approved escalation ticket that authorised it (optional but
    recommended — it's how the operator audit timeline correlates the
    two events)."""

    amount_usdc: float = Field(gt=0)
    freelancer_id: str = Field(min_length=1)
    escalation_id: str | None = None


class RegisterFreelancerRequest(BaseModel):
    """Owner-onboarded fiat destination. ``fiat_destination`` is the
    ramp-specific id (Stripe Connect account, MoonPay payout token)."""

    owner_subject: str = Field(min_length=1)
    freelancer_id: str = Field(min_length=1)
    fiat_destination: str = Field(min_length=1)
    display_name: str = ""
    kyc_status: str = "unverified"
    metadata: dict[str, Any] | None = None


class DarwinFiatRefundRequest(BaseModel):
    """Recovery for a failed fiat payout: refund the leg-1 USDC from
    the omnibus address back to the Darwin's wallet."""

    amount_usdc: float = Field(gt=0)
    reason: str = "fiat_payout_leg2_failed"


class PushSubscriptionRequest(BaseModel):
    """A W3C `PushSubscription` the PWA hands back after
    :meth:`PushManager.subscribe`. ``endpoint`` + ``keys.{p256dh,auth}``
    are everything the server needs to deliver a VAPID-signed, ECE-
    encrypted push to the registering browser."""

    endpoint: str = Field(min_length=1)
    p256dh: str = Field(min_length=1)
    auth: str = Field(min_length=1)
    user_agent: str = ""

    # Owner-driven revoke uses ?endpoint=… on DELETE (proxies strip DELETE
    # bodies often enough that the query form is more reliable). Kept as a
    # comment for spec-by-grep — there's no body DTO for that route.


class RetryOmnibusFundRequest(BaseModel):
    """Operator retry of a fund_child that was skipped at spawn time
    (chain hiccup, transient outage)."""

    amount_usdc: float = Field(gt=0)


class OmnibusCreditRequest(BaseModel):
    """Operator credit of a user's omnibus balance for an already-settled
    deposit (testnet faucet top-up, reconciled wire) — the non-Stripe entry
    to the credit a spawn's omnibus funding debits."""

    user_subject: str = Field(min_length=1)
    amount_usdc: float = Field(gt=0)
    source_ref: str | None = None


class FiatWithdrawRequest(BaseModel):
    amount_usdc: float = Field(gt=0)
    destination: str = Field(min_length=1)


class RotateSigningKeyRequest(BaseModel):
    drain_window_seconds: float | None = Field(default=None, gt=0)


class RejectProposalRequest(BaseModel):
    reason: str = "operator rejected"


class HarvestRuleRequest(BaseModel):
    destination: str = Field(min_length=1)
    trigger_balance_usdc: float = Field(gt=0)
    amount_usdc: float | None = Field(default=None, gt=0)
    interval_seconds: float | None = Field(default=None, ge=60)


class VaultPutRequest(BaseModel):
    tier: str = Field(pattern=r"^(public|scoped|sensitive)$")
    key: str = Field(min_length=1)
    value: str
    scope: str | None = None
    ttl_seconds: float | None = Field(default=None, gt=0)
    original_filename: str | None = None


class WalletPolicyRequest(BaseModel):
    per_action_cap_usdc: float | None = Field(default=None, ge=0)
    per_day_cap_usdc: float | None = Field(default=None, ge=0)
    per_lifetime_cap_usdc: float | None = Field(default=None, ge=0)


class PreApprovalRequest(BaseModel):
    """Owner pre-approval rule body (doc 17 §"Darwin paying a human").
    The rule_id is the URL parameter — this body carries the match
    criteria + lifetime. ``max_amount_usdc`` is required; the other
    fields are conjunctive filters. ``max_uses`` caps how many spends
    the rule may clear before becoming inert; null is unlimited."""

    max_amount_usdc: float = Field(gt=0)
    counterparty_category: str | None = None
    counterparty_substring: str | None = None
    expires_at: float | None = Field(default=None, gt=0)
    max_uses: int | None = Field(default=None, ge=1)


class ThrottleRequest(BaseModel):
    tier: str = Field(pattern=r"^(fast|reasoning|coding|image_gen|embedding)$")
    max_daily_calls: int = Field(ge=0)


class SpendLockRequest(BaseModel):
    locked: bool
    reason: str = "owner spend-lock"


class ReproductionBlockRequest(BaseModel):
    blocked: bool


class DisabledNeuronsRequest(BaseModel):
    neurons: list[str] = Field(default_factory=list)


class DisabledCategoriesRequest(BaseModel):
    categories: list[str] = Field(default_factory=list)


class HarvestRequest(BaseModel):
    amount_usdc: float = Field(gt=0)
    destination: str = Field(min_length=1)


class PayoutAddressRequest(BaseModel):
    """Checksummed USDC address (e.g. an exchange deposit address) for this
    Darwin's harvests and closing sweep."""

    address: str = Field(min_length=42, max_length=42)


class AmendmentRequest(BaseModel):
    """A platform amendment layer to ship via forced reproduction (doc 02).

    ``amendment_blob_b64`` is the layer bytes base64-encoded so the JSON
    body is text-clean. ``amendment_name`` is the layer's filename inside
    the manifest (e.g. ``layer_5_amendment_kyc_required``). ``kill_parent_reason``
    annotates the kill audit entry that follows successful reproduction.
    """

    amendment_blob_b64: str
    amendment_name: str = Field(min_length=1)
    kill_parent_reason: str = "amendment reproduction"


# -- typed responses (so the dashboard's generated client knows the shapes) --
class BillingInfrastructure(BaseModel):
    gross_usdc: float
    provider_cost_usdc: float
    platform_revenue_usdc: float
    events: int


class BillingHarvests(BaseModel):
    gross_usdc: float
    net_usdc: float
    fee_usdc: float
    events: int
    history_tail: list[dict[str, Any]] = Field(default_factory=list)


class BillingSummary(BaseModel):
    darwin_id: str
    infrastructure: BillingInfrastructure
    harvests: BillingHarvests
    total_platform_revenue_usdc: float


class LineageAncestor(BaseModel):
    darwin_id: str
    alive: bool


class LineageNode(BaseModel):
    darwin_id: str
    budget_usdc: float
    spawned_at: float
    alive: bool
    children: list["LineageNode"] = Field(default_factory=list)


class LineageTree(BaseModel):
    darwin_id: str
    generation: int
    ancestors: list[LineageAncestor]
    descendants: list[LineageNode]


class AlertRow(BaseModel):
    darwin_id: str
    kind: str
    severity: str
    suggested_level: int
    detail: dict[str, Any] = Field(default_factory=dict)
    ts: float


class DarwinAlerts(BaseModel):
    darwin_id: str
    alerts: list[AlertRow]
