"""Platform omnibus wallet (docs/engineering/22 §"The full payment stack").

The omnibus wallet is the platform's own on-chain wallet. It sits
between the user (fiat → USDC via Stripe) and the per-Darwin
Coinbase Agentic Wallets:

* **Inbound**: a confirmed Stripe payment is recorded as a USDC
  credit on the omnibus wallet (the actual on-chain mint can lag —
  the platform pre-funds the omnibus and uses it as float).
* **Fund**: the supervisor moves USDC out of the omnibus to a child
  Darwin wallet at spawn time. The transfer is screened by sanctions
  and recorded in the omnibus ledger.
* **Sweep**: when a Darwin dies with residual balance, the supervisor
  sweeps it back to the omnibus, then credits the user's account for
  withdrawal. The user's running balance is tracked here too.
* **Reconcile**: daily, the omnibus balance + sum of per-Darwin
  wallets + outstanding user credits must equal the lifetime fiat
  deposits − lifetime withdrawals − platform revenue (markup + fees).
  A mismatch fires a CRITICAL alert.

This module is the orchestration layer; the on-chain bits delegate to
the same :class:`ChainClient` Protocol the per-Darwin
:class:`RealWallet` uses (docs/engineering/17).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable


class OmnibusError(Exception):
    """Omnibus operation refused or failed. The caller surfaces this as
    a user-facing refusal rather than retrying blindly."""


class OmnibusEntryKind(str, Enum):
    DEPOSIT = "deposit"           # incoming fiat → USDC credit
    FUND_CHILD = "fund_child"     # outgoing to per-Darwin wallet
    SWEEP_CHILD = "sweep_child"   # incoming from dead-Darwin sweep
    WITHDRAW_USER = "withdraw_user"  # outgoing to user's external wallet
    PLATFORM_REVENUE = "platform_revenue"  # carved off harvests + markup
    ADJUSTMENT = "adjustment"     # manual correction (audit-logged)
    # Reproduction relay: parent's transfer_out_for_child sends the
    # surplus to the omnibus address; this second leg relays it to the
    # newly-spawned child's own address. Distinct from FUND_CHILD because
    # the source is the parent's wallet (already on-chain), not a user's
    # credit balance — so user_credits is NOT touched.
    RELAY_REPRODUCTION = "relay_reproduction"
    # Darwin → fiat: the Darwin's wallet debits USDC to the omnibus
    # address (first leg, on-chain); the platform converts that USDC into
    # fiat via the configured FiatRamp and pushes the payout to a verified
    # bank or fiat destination (second leg). Like RELAY_REPRODUCTION, the
    # source is a Darwin's wallet — user_credits is NOT touched. Used when
    # a freelancer / legal / professional counterparty only accepts fiat.
    DARWIN_FIAT_PAYOUT = "darwin_fiat_payout"
    # Refund: when a Darwin → fiat payout fails on leg 2 (omnibus → fiat),
    # leg 1 has already moved USDC from the Darwin's wallet to the omnibus
    # address. This kind records the recovery: a chain transfer from the
    # omnibus back to the Darwin's wallet undoing leg 1, so the Darwin's
    # treasury reflects reality and the operator's reconciliation closes.
    REFUND_TO_DARWIN = "refund_to_darwin"


@dataclass(frozen=True)
class OmnibusEntry:
    """One movement on the omnibus ledger. Immutable; the ledger is
    append-only. Movements always carry the actor (operator subject) for
    forensic replay."""

    kind: OmnibusEntryKind
    amount_usdc: float
    counterparty: str            # darwin_id or user_subject or "stripe:..." etc.
    actor: str
    ts: float
    memo: str = ""
    tx_hash: str = ""            # on-chain hash when applicable (sweeps, funds)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value, "amount_usdc": self.amount_usdc,
            "counterparty": self.counterparty, "actor": self.actor,
            "ts": self.ts, "memo": self.memo, "tx_hash": self.tx_hash,
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class OmnibusChainClient(Protocol):
    """The minimum surface :class:`OmnibusWallet` needs to move USDC
    on chain. Compatible with :class:`darwin_platform.adapters.wallet
    .ChainClient`; production wires the same :class:`Web3ChainClient`
    instance pointed at the omnibus address."""

    def address(self) -> str: ...
    def balance_usdc(self) -> float: ...
    def transfer(self, *, to: str, amount_usdc: float, memo: str) -> dict: ...


@dataclass
class OmnibusBalances:
    """A snapshot used by the daily reconciliation. Each field is a
    float USDC amount; the discrepancy is what triggers the CRITICAL
    alert when above the tolerance."""

    on_chain_omnibus: float
    sum_of_darwin_wallets: float
    sum_of_user_credits: float
    lifetime_deposits: float
    lifetime_withdrawals: float
    lifetime_platform_revenue: float

    def expected_omnibus_held(self) -> float:
        """The platform-side identity: what the omnibus *should* be
        holding given everything we know."""
        # deposits in - withdrawals out - platform_revenue carved off
        # - per-Darwin wallet outflows + per-Darwin sweep inflows ≈
        # the omnibus's running balance. The simplification here is
        # that ``deposits - withdrawals - platform_revenue`` is the
        # net float; per-Darwin wallets are tracked separately and
        # added in.
        return (
            self.lifetime_deposits
            - self.lifetime_withdrawals
            - self.lifetime_platform_revenue
            - self.sum_of_darwin_wallets
            - self.sum_of_user_credits
        )

    def discrepancy(self) -> float:
        """Positive ⇒ on-chain has more than the platform's books say."""
        return round(self.on_chain_omnibus - self.expected_omnibus_held(), 6)


@dataclass
class ReconciliationResult:
    """One reconciliation snapshot. The supervisor publishes this on
    the audit log + the operator dashboard."""

    balances: OmnibusBalances
    tolerance_usdc: float
    ok: bool
    discrepancy_usdc: float
    note: str
    ts: float

    def to_json(self) -> dict[str, Any]:
        return {
            "balances": {
                "on_chain_omnibus": self.balances.on_chain_omnibus,
                "sum_of_darwin_wallets": self.balances.sum_of_darwin_wallets,
                "sum_of_user_credits": self.balances.sum_of_user_credits,
                "lifetime_deposits": self.balances.lifetime_deposits,
                "lifetime_withdrawals": self.balances.lifetime_withdrawals,
                "lifetime_platform_revenue": self.balances.lifetime_platform_revenue,
            },
            "expected_omnibus_held": self.balances.expected_omnibus_held(),
            "tolerance_usdc": self.tolerance_usdc,
            "ok": self.ok,
            "discrepancy_usdc": self.discrepancy_usdc,
            "note": self.note, "ts": self.ts,
        }


class OmnibusWallet:
    """Platform's own wallet + user-credit ledger.

    The class is small on purpose: it does NOT decide who can fund or
    withdraw (the supervisor + KYC gate own that); it does NOT settle
    chain transfers asynchronously (the :class:`OmnibusChainClient`
    Protocol decides whether ``transfer`` blocks for confirmation).
    Its job is to keep the platform's books accurate and to refuse any
    operation that would leave the books inconsistent.

    Sanctions are accepted as a callable so the omnibus and per-Darwin
    wallets can share a fleet-wide screener instance.
    """

    def __init__(
        self,
        chain: OmnibusChainClient,
        *,
        clock: Callable[[], float] = time.time,
        sanctions: Any | None = None,
        reconciliation_tolerance_usdc: float = 1.0,
    ) -> None:
        if not isinstance(chain, OmnibusChainClient):
            raise TypeError("OmnibusWallet requires an OmnibusChainClient")
        self._chain = chain
        self._clock = clock
        self._sanctions = sanctions
        self._tolerance = float(reconciliation_tolerance_usdc)
        self._ledger: list[OmnibusEntry] = []
        # Per-user running credit balance (Stripe deposits + dead-Darwin
        # sweeps − withdrawals). The platform owes this on the omnibus.
        self._user_credits: dict[str, float] = {}
        # Lifetime aggregates for reconciliation.
        self._lifetime_deposits = 0.0
        self._lifetime_withdrawals = 0.0
        self._lifetime_platform_revenue = 0.0

    # -- read-side ---------------------------------------------------------
    def address(self) -> str:
        return self._chain.address()

    def balance_usdc(self) -> float:
        return float(self._chain.balance_usdc())

    def user_credit(self, subject: str) -> float:
        return float(self._user_credits.get(subject, 0.0))

    def ledger(self) -> list[OmnibusEntry]:
        return list(self._ledger)

    def lifetime_totals(self) -> dict[str, float]:
        return {
            "deposits": round(self._lifetime_deposits, 6),
            "withdrawals": round(self._lifetime_withdrawals, 6),
            "platform_revenue": round(self._lifetime_platform_revenue, 6),
        }

    # -- write-side --------------------------------------------------------
    def record_deposit(
        self, *, user_subject: str, amount_usdc: float,
        source_ref: str, actor: str = "platform.stripe",
    ) -> OmnibusEntry:
        """Record a confirmed fiat→USDC deposit. The chain credit is
        assumed to have settled (the caller is the Stripe webhook
        handler reconciling against the omnibus address)."""
        amount = _require_positive(amount_usdc, "deposit amount")
        if not user_subject:
            raise OmnibusError("deposit requires user_subject")
        entry = self._append(
            OmnibusEntry(
                kind=OmnibusEntryKind.DEPOSIT,
                amount_usdc=amount, counterparty=user_subject,
                actor=actor, ts=self._clock(), memo=source_ref,
                metadata={"source_ref": source_ref},
            )
        )
        self._user_credits[user_subject] = self.user_credit(user_subject) + amount
        self._lifetime_deposits += amount
        return entry

    def fund_child(
        self, *, darwin_id: str, to_address: str, amount_usdc: float,
        user_subject: str, actor: str = "platform.supervisor",
    ) -> OmnibusEntry:
        """Move USDC from the omnibus to a per-Darwin wallet. Debits
        the user's credit ledger so the deposit is now "live" inside
        the Darwin's wallet rather than parked on the omnibus."""
        amount = _require_positive(amount_usdc, "fund amount")
        if not to_address:
            raise OmnibusError("fund_child requires to_address")
        if self.user_credit(user_subject) + 1e-9 < amount:
            raise OmnibusError(
                f"user {user_subject!r} credit "
                f"${self.user_credit(user_subject):.4f} insufficient for "
                f"${amount:.4f} fund"
            )
        if amount > self.balance_usdc() + 1e-9:
            raise OmnibusError(
                f"omnibus balance ${self.balance_usdc():.4f} insufficient "
                f"for ${amount:.4f}"
            )
        self._screen(to_address)
        try:
            tx = self._chain.transfer(
                to=to_address, amount_usdc=amount,
                memo=f"fund:{darwin_id}",
            )
        except Exception as exc:
            raise OmnibusError(f"omnibus chain transfer failed: {exc}") from exc
        self._user_credits[user_subject] -= amount
        entry = self._append(OmnibusEntry(
            kind=OmnibusEntryKind.FUND_CHILD,
            amount_usdc=amount, counterparty=darwin_id, actor=actor,
            ts=self._clock(),
            memo=f"user={user_subject}",
            tx_hash=str((tx or {}).get("tx_hash", "")),
            metadata={"user_subject": user_subject,
                      "to_address": to_address},
        ))
        return entry

    def relay_reproduction(
        self, *,
        parent_id: str,
        child_id: str,
        child_address: str,
        amount_usdc: float,
        actor: str = "platform.supervisor",
    ) -> OmnibusEntry:
        """Second leg of the reproduction transfer: relay the parent's
        surplus from the omnibus address (where the parent's
        ``transfer_out_for_child`` deposited it) to the newly-spawned
        child's on-chain address. Does NOT touch ``user_credits`` —
        the funds came from the parent's wallet, not a user deposit, so
        the omnibus's user-attributed books are unchanged. Refuses on
        insufficient on-chain balance and sanctions-screens the
        recipient just like ``fund_child``."""
        amount = _require_positive(amount_usdc, "reproduction relay amount")
        if not child_address:
            raise OmnibusError("relay_reproduction requires child_address")
        if amount > self.balance_usdc() + 1e-9:
            raise OmnibusError(
                f"omnibus balance ${self.balance_usdc():.4f} insufficient "
                f"for ${amount:.4f} reproduction relay",
            )
        self._screen(child_address)
        try:
            tx = self._chain.transfer(
                to=child_address, amount_usdc=amount,
                memo=f"reproduction:{parent_id}->{child_id}",
            )
        except Exception as exc:
            raise OmnibusError(
                f"reproduction relay chain transfer failed: {exc}"
            ) from exc
        return self._append(OmnibusEntry(
            kind=OmnibusEntryKind.RELAY_REPRODUCTION,
            amount_usdc=amount, counterparty=child_id, actor=actor,
            ts=self._clock(),
            memo=f"parent={parent_id}",
            tx_hash=str((tx or {}).get("tx_hash", "")),
            metadata={"parent_id": parent_id,
                      "child_id": child_id,
                      "child_address": child_address},
        ))

    def relay_fiat_payout(
        self, *,
        darwin_id: str,
        amount_usdc: float,
        fiat_destination: str,
        fiat_ramp: Any,
        actor: str = "platform.supervisor",
        escalation_id: str | None = None,
    ) -> OmnibusEntry:
        """Second leg of the Darwin → fiat payout flow. The Darwin's USDC
        wallet has already debited the amount to the omnibus address
        on-chain (first leg, via ``wallet.transfer_out_for_fiat_payout``).
        This call converts that USDC into fiat via the injected ramp and
        ships the payout to the verified destination.

        Does NOT touch ``user_credits`` — the source of funds is the
        Darwin's wallet, not a user deposit. Sanctions-screens the
        destination and refuses on screener verdict. The ramp's
        ``withdraw`` is wrapped so a backend failure surfaces as
        :class:`OmnibusError` for caller-uniform handling."""
        amount = _require_positive(amount_usdc, "fiat payout amount")
        if not fiat_destination:
            raise OmnibusError("relay_fiat_payout requires fiat_destination")
        if fiat_ramp is None:
            raise OmnibusError("relay_fiat_payout requires a fiat_ramp")
        self._screen(fiat_destination)
        try:
            receipt = fiat_ramp.withdraw(
                amount_usdc=amount, destination=fiat_destination,
            )
        except Exception as exc:
            raise OmnibusError(
                f"fiat ramp withdraw failed: {exc}"
            ) from exc
        provider_tx_id = getattr(receipt, "provider_tx_id", "") or ""
        return self._append(OmnibusEntry(
            kind=OmnibusEntryKind.DARWIN_FIAT_PAYOUT,
            amount_usdc=amount, counterparty=darwin_id, actor=actor,
            ts=self._clock(),
            memo=f"fiat:{fiat_destination}",
            tx_hash=str(provider_tx_id),
            metadata={
                "darwin_id": darwin_id,
                "fiat_destination": fiat_destination,
                "provider": getattr(receipt, "provider", ""),
                "amount_usd": float(getattr(receipt, "amount_usd", 0.0)),
                "fee_usd": float(getattr(receipt, "fee_usd", 0.0)),
                # Link back to the approved escalation ticket so the
                # operator timeline shows which authorisation each fiat
                # hop settled. None when called outside an escalation flow.
                "escalation_id": escalation_id,
            },
        ))

    def refund_to_darwin(
        self, *,
        darwin_id: str,
        darwin_address: str,
        amount_usdc: float,
        reason: str = "",
        actor: str = "platform.supervisor",
    ) -> OmnibusEntry:
        """Send USDC back from the omnibus to a Darwin's on-chain address.

        Recovery path for a failed Darwin → fiat payout: leg 1 succeeded
        (the Darwin's wallet debited to the omnibus) but leg 2 failed
        (omnibus → fiat ramp). Without this, the Darwin's USDC is parked
        at the omnibus address and the operator has to reconcile by hand.

        Does NOT touch ``user_credits`` — the funds being returned came
        from the Darwin's wallet, not a user deposit. The chain submit is
        sanctions-screened (the destination is the Darwin's own address,
        so a real screener returns clean; a fake one in tests respects
        the same shape)."""
        amount = _require_positive(amount_usdc, "refund amount")
        if not darwin_address:
            raise OmnibusError("refund_to_darwin requires darwin_address")
        if amount > self.balance_usdc() + 1e-9:
            raise OmnibusError(
                f"omnibus balance ${self.balance_usdc():.4f} insufficient "
                f"for ${amount:.4f} refund",
            )
        self._screen(darwin_address)
        try:
            tx = self._chain.transfer(
                to=darwin_address, amount_usdc=amount,
                memo=f"refund:{darwin_id}",
            )
        except Exception as exc:
            raise OmnibusError(
                f"refund chain transfer failed: {exc}"
            ) from exc
        return self._append(OmnibusEntry(
            kind=OmnibusEntryKind.REFUND_TO_DARWIN,
            amount_usdc=amount, counterparty=darwin_id, actor=actor,
            ts=self._clock(),
            memo=reason or "",
            tx_hash=str((tx or {}).get("tx_hash", "")),
            metadata={
                "darwin_id": darwin_id,
                "darwin_address": darwin_address,
                "reason": reason,
            },
        ))

    def sweep_child(
        self, *, darwin_id: str, amount_usdc: float, user_subject: str,
        from_address: str = "",
        actor: str = "platform.supervisor",
    ) -> OmnibusEntry:
        """Record a residual-balance sweep from a dead Darwin back to
        the omnibus. The on-chain transfer was initiated by the
        per-Darwin wallet's ``transfer_out_for_child`` (or its
        platform-side counterpart); this method only records the
        platform-side accounting."""
        amount = _require_positive(amount_usdc, "sweep amount")
        self._user_credits[user_subject] = self.user_credit(user_subject) + amount
        return self._append(OmnibusEntry(
            kind=OmnibusEntryKind.SWEEP_CHILD,
            amount_usdc=amount, counterparty=darwin_id, actor=actor,
            ts=self._clock(),
            memo=f"user={user_subject}",
            metadata={"user_subject": user_subject,
                      "from_address": from_address},
        ))

    def withdraw_to_user(
        self, *, user_subject: str, amount_usdc: float, to_address: str,
        fee_usdc: float = 0.0, actor: str = "platform.harvest",
    ) -> OmnibusEntry:
        """User-initiated withdrawal. Refuses on insufficient credit or
        insufficient on-chain balance; sanctions-screens the recipient."""
        amount = _require_positive(amount_usdc, "withdraw amount")
        fee = max(0.0, float(fee_usdc))
        gross = amount + fee
        if self.user_credit(user_subject) + 1e-9 < gross:
            raise OmnibusError(
                f"user {user_subject!r} credit "
                f"${self.user_credit(user_subject):.4f} insufficient for "
                f"${gross:.4f} (amount+fee)"
            )
        if gross > self.balance_usdc() + 1e-9:
            raise OmnibusError(
                f"omnibus balance insufficient for ${gross:.4f} withdraw"
            )
        self._screen(to_address)
        try:
            tx = self._chain.transfer(
                to=to_address, amount_usdc=amount,
                memo=f"withdraw:{user_subject}",
            )
        except Exception as exc:
            raise OmnibusError(f"withdraw chain transfer failed: {exc}") from exc
        self._user_credits[user_subject] -= gross
        self._lifetime_withdrawals += amount
        if fee > 0:
            self._lifetime_platform_revenue += fee
        entry = self._append(OmnibusEntry(
            kind=OmnibusEntryKind.WITHDRAW_USER,
            amount_usdc=amount, counterparty=user_subject, actor=actor,
            ts=self._clock(),
            memo=to_address,
            tx_hash=str((tx or {}).get("tx_hash", "")),
            metadata={"fee_usdc": fee, "to_address": to_address},
        ))
        if fee > 0:
            self._append(OmnibusEntry(
                kind=OmnibusEntryKind.PLATFORM_REVENUE,
                amount_usdc=fee, counterparty="platform.harvest_fee",
                actor=actor, ts=self._clock(),
                memo=f"harvest_fee:{user_subject}",
            ))
        return entry

    def record_platform_revenue(
        self, *, amount_usdc: float, source: str, actor: str = "platform.billing",
    ) -> OmnibusEntry:
        """Markup pass-through (LiteLLM, E2B, Browserbase) accrues here.
        The per-Darwin wallet has already paid the gross; the supervisor
        carves the markup into platform_revenue for accounting."""
        amount = _require_positive(amount_usdc, "platform revenue")
        self._lifetime_platform_revenue += amount
        return self._append(OmnibusEntry(
            kind=OmnibusEntryKind.PLATFORM_REVENUE,
            amount_usdc=amount, counterparty=source, actor=actor,
            ts=self._clock(),
            memo=source,
        ))

    # -- reconciliation ----------------------------------------------------
    def reconcile(
        self, *,
        sum_of_darwin_wallets: float,
    ) -> ReconciliationResult:
        """Compute the discrepancy between on-chain reality and the
        platform's books. Caller passes the summed per-Darwin balances
        (the supervisor walks its handle map). The omnibus doesn't
        observe per-Darwin wallets directly."""
        sum_credits = sum(self._user_credits.values())
        balances = OmnibusBalances(
            on_chain_omnibus=self.balance_usdc(),
            sum_of_darwin_wallets=float(sum_of_darwin_wallets),
            sum_of_user_credits=sum_credits,
            lifetime_deposits=self._lifetime_deposits,
            lifetime_withdrawals=self._lifetime_withdrawals,
            lifetime_platform_revenue=self._lifetime_platform_revenue,
        )
        disc = balances.discrepancy()
        ok = abs(disc) <= self._tolerance + 1e-9
        return ReconciliationResult(
            balances=balances, tolerance_usdc=self._tolerance,
            ok=ok, discrepancy_usdc=disc,
            note=(
                "books in balance" if ok
                else f"discrepancy ${disc:.4f} > tolerance ${self._tolerance:.4f}"
            ),
            ts=self._clock(),
        )

    # -- internals ---------------------------------------------------------
    def _screen(self, recipient: str) -> None:
        if self._sanctions is None:
            return
        try:
            verdict = self._sanctions.check(str(recipient))
        except Exception as exc:
            raise OmnibusError(f"sanctions screener errored: {exc}") from exc
        if not verdict.allowed:
            raise OmnibusError(
                f"sanctions:{verdict.source} refused recipient "
                f"{recipient!r}: {verdict.reason or 'screening refused'}"
            )

    def _append(self, entry: OmnibusEntry) -> OmnibusEntry:
        self._ledger.append(entry)
        return entry


def _require_positive(value: float, label: str) -> float:
    v = float(value)
    if v <= 0:
        raise OmnibusError(f"{label} must be positive, got {v}")
    return v
