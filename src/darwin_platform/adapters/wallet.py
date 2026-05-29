"""Real wallet adapter (docs/engineering/17).

The genome's :class:`WalletPort` is small (balance / spent counters / pay).
The Supervisor's fakes implement a wider surface (debit / credit / movements
/ harvest / set_policy / owner_soft_caps) because the platform layer reads
those for billing, ledger reconciliation, and the doc-08 wallet-policy
endpoint. A real adapter has to match that surface too — otherwise swapping
it in for ``FakeWallet`` would silently lose features.

This module ships:

- :class:`ChainClient` — Protocol around the on-chain bits that vary by
  network (Base Sepolia, Base mainnet, etc.). Tests inject a stub; real
  deployments inject :class:`Web3ChainClient` (gated behind the optional
  ``web3`` dep — not built here yet, but the Protocol is what a real
  wrapper plugs into).
- :class:`RealWallet` — :class:`WalletPort`-conformant adapter that runs
  real on-chain transfers via the ``ChainClient`` for ``pay`` /
  ``harvest_to_owner`` / ``transfer_out_for_child``, while keeping the
  per-day / per-lifetime / recent-spend / movements / soft-cap
  bookkeeping locally (those are platform-side accounting concerns
  independent of the chain).

Production deployers wire a real ``ChainClient`` (web3.py against Base
Sepolia testnet for development, mainnet for production) and an HSM- or
KMS-held signing key. The signing private material **never** enters this
process — it lives in the ChainClient's secured backend.
"""

from __future__ import annotations

from collections import deque
from typing import Protocol, runtime_checkable

from darwin_dna.constitution import OwnerSoftCaps, PreApprovalRule
from darwin_dna.hyperparameters import HARD_CAPS


class WalletError(Exception):
    """Same shape as :class:`darwin_fakes.fake_wallet.WalletError` so the
    genome's payments_facilitator handles refusals identically."""


@runtime_checkable
class ChainClient(Protocol):
    """The on-chain bits that vary by network. Real impls wrap web3.py /
    the Coinbase CDP SDK / etc. ``address()`` is the wallet's own
    on-chain address (the recipient for incoming credits)."""

    def address(self) -> str: ...

    def balance_usdc(self) -> float: ...

    def transfer(self, *, to: str, amount_usdc: float, memo: str) -> dict:
        """Submit a USDC transfer. Returns ``{"tx_hash": str, ...}``."""
        ...


@runtime_checkable
class ConfirmingChainClient(Protocol):
    """Optional Phase 1 extension: chain clients that can wait for an
    on-chain confirmation should implement this so :class:`RealWallet`
    can block updating the local ledger until the transfer is final.

    Implementations should *raise* on timeout or failed confirmation so
    the wallet does not credit a transaction that was reorged out.
    """

    def address(self) -> str: ...
    def balance_usdc(self) -> float: ...
    def transfer(self, *, to: str, amount_usdc: float, memo: str) -> dict: ...
    def wait_for_confirmation(
        self, tx_hash: str, *, timeout_s: float = 60.0, min_confirmations: int = 1,
    ) -> dict:
        """Block until the tx is confirmed; return the receipt. Raise on
        timeout / failure. ``min_confirmations`` defaults to 1 for fast
        chains (Base, Arbitrum); production may bump to 6 for mainnet
        Ethereum."""
        ...


# Known networks the adapter understands. Mainnet is gated by an explicit
# constructor argument so a misconfigured factory cannot accidentally use
# real-money credentials in a test path.
NETWORK_BASE_SEPOLIA = "base-sepolia"
NETWORK_BASE_MAINNET = "base-mainnet"
NETWORK_ETH_MAINNET = "eth-mainnet"
KNOWN_NETWORKS = frozenset({
    NETWORK_BASE_SEPOLIA, NETWORK_BASE_MAINNET, NETWORK_ETH_MAINNET,
})
MAINNET_NETWORKS = frozenset({NETWORK_BASE_MAINNET, NETWORK_ETH_MAINNET})


class RealWallet:
    """:class:`WalletPort`-conformant adapter that fronts a real chain. The
    bookkeeping (spent_today / spent_lifetime / recent_spend / movement
    log / soft caps) is held locally — it is platform accounting that the
    chain doesn't owe us. The real chain owns the authoritative balance;
    we expose it via ``balance_usdc``.

    Movements feed: every successful chain operation appends a row to the
    local ledger in the same shape as :class:`FakeWallet.movements` so the
    genome's perceive node can journal + reconcile against it unchanged.
    """

    def __init__(
        self,
        chain: ChainClient,
        *,
        clock,
        sanctions=None,
        network: str = NETWORK_BASE_SEPOLIA,
        wait_for_confirmation: bool = False,
        confirmation_timeout_s: float = 60.0,
        min_confirmations: int = 1,
    ) -> None:
        if not isinstance(chain, ChainClient):
            raise TypeError("RealWallet requires a ChainClient")
        if network not in KNOWN_NETWORKS:
            raise ValueError(
                f"unknown network {network!r}; "
                f"expected one of {sorted(KNOWN_NETWORKS)}"
            )
        self._chain = chain
        self._clock = clock
        # Phase 1: explicit mainnet awareness. A wallet on a mainnet
        # network refuses transfers that haven't been confirmed when
        # ``wait_for_confirmation`` is set — the platform must not credit
        # the local ledger for a tx that could be reorged out.
        self._network = network
        self._is_mainnet = network in MAINNET_NETWORKS
        self._wait_for_confirmation = bool(wait_for_confirmation)
        self._confirmation_timeout_s = float(confirmation_timeout_s)
        self._min_confirmations = max(1, int(min_confirmations))
        # Optional sanctions screener (doc 17). When set, every outbound
        # transfer is checked against the screener BEFORE the chain submit;
        # a refusal raises WalletError so the genome's treasury layer sees
        # the same refusal shape it would for any cap breach.
        self._sanctions = sanctions
        self._spent_today = 0.0
        self._spent_lifetime = 0.0
        self._today = self._day()
        self._recent: deque[float] = deque(maxlen=16)
        self._last_revenue_ts = float(clock())
        self.ledger: list[dict] = []
        self._per_action_cap: float = HARD_CAPS.per_action_usdc
        self._per_day_cap: float = HARD_CAPS.per_day_usdc
        self._per_lifetime_cap: float = HARD_CAPS.per_lifetime_usdc
        # Owner pre-approval rules. Keyed by rule_id; the genome's
        # facilitator reads ``owner_preapprovals()`` and forwards them
        # into the policy engine so a matching low-friction freelancer /
        # legal_financial / over-threshold spend skips escalation.
        self._preapprovals: dict[str, PreApprovalRule] = {}
        self._preapproval_persister = None

    def _day(self) -> str:
        import time as _t
        return _t.strftime("%Y-%m-%d", _t.gmtime(self._clock()))

    def _roll(self) -> None:
        d = self._day()
        if d != self._today:
            self._today = d
            self._spent_today = 0.0

    # -- WalletPort (read) -------------------------------------------------
    def balance_usdc(self) -> float:
        """Chain is the authoritative source. The platform's bookkeeping
        is for accounting only; it must NOT be the spend-decision input."""
        return float(self._chain.balance_usdc())

    def movements(self) -> list[dict]:
        return self.ledger

    def spent_today_usdc(self) -> float:
        self._roll()
        return round(self._spent_today, 6)

    def spent_lifetime_usdc(self) -> float:
        return round(self._spent_lifetime, 6)

    def recent_spend_usdc(self) -> tuple[float, ...]:
        return tuple(self._recent)

    def seconds_since_revenue(self) -> float:
        return max(0.0, float(self._clock()) - self._last_revenue_ts)

    def address(self) -> str:
        return self._chain.address()

    @property
    def network(self) -> str:
        return self._network

    @property
    def is_mainnet(self) -> bool:
        return self._is_mainnet

    # -- WalletPort (write) -- platform-side caps + chain submit ----------
    def pay(self, *, to: str, amount_usdc: float, memo: str) -> dict:
        return self._debit(amount_usdc, to, memo, kind="payment")

    def debit(self, amount_usdc: float, memo: str) -> None:
        """Metered gateway debit (LLM tokens, compute) — the platform's
        gateway billing the Darwin's virtual key. Doesn't go through
        treasury_policy (it's a utility bill, like FakeWallet's debit)."""
        self._debit(amount_usdc, "platform.gateway", memo, kind="metered")

    def _debit(
        self, amount_usdc: float, to: str, memo: str, *, kind: str
    ) -> dict:
        self._roll()
        amount = float(amount_usdc)
        if amount < 0:
            raise WalletError("negative debit")
        cap = min(HARD_CAPS.per_action_usdc, self._per_action_cap)
        if amount > cap:
            raise WalletError(f"wallet hard cap: ${amount:.2f} > ${cap:.2f}")
        day_cap = min(HARD_CAPS.per_day_usdc, self._per_day_cap)
        if self._spent_today + amount > day_cap + 1e-9:
            raise WalletError(
                f"wallet day cap: ${self._spent_today + amount:.2f} > ${day_cap:.2f}"
            )
        life_cap = min(HARD_CAPS.per_lifetime_usdc, self._per_lifetime_cap)
        if self._spent_lifetime + amount > life_cap + 1e-9:
            raise WalletError(
                f"wallet lifetime cap: ${self._spent_lifetime + amount:.2f} > ${life_cap:.2f}"
            )
        # Balance check against the chain — authoritative.
        if amount > self._chain.balance_usdc() + 1e-9:
            raise WalletError(
                f"insufficient on-chain funds: ${amount:.2f}"
            )
        # Sanctions screening (doc 17). Refused recipients never reach the
        # chain. Failures inside the screener must surface as a closed-fail
        # (the screener returns ``allowed=False``) — we never bypass it
        # because of a backend error.
        if self._sanctions is not None:
            verdict = self._sanctions.check(str(to))
            if not verdict.allowed:
                raise WalletError(
                    f"sanctions:{verdict.source} refused recipient "
                    f"{to!r}: {verdict.reason or 'screening refused'}"
                )
        # Submit to the chain. A failure here MUST NOT update bookkeeping
        # (otherwise the platform thinks the Darwin spent money it
        # didn't actually move on-chain).
        try:
            tx = self._chain.transfer(to=to, amount_usdc=amount, memo=memo)
        except Exception as exc:
            raise WalletError(f"chain transfer failed: {exc}") from exc
        # Phase 1 mainnet safety (doc 21 §"Mainnet safety"): wait for the
        # tx to confirm before crediting the local ledger. Skipped when
        # the chain client doesn't expose ``wait_for_confirmation`` so
        # testnet stubs and the in-memory FakeChainClient keep working.
        tx_hash = tx.get("tx_hash", "") if isinstance(tx, dict) else ""
        confirmation = self._confirm_if_required(tx_hash)
        self._spent_today += amount
        self._spent_lifetime += amount
        if amount > 0:
            self._recent.append(amount)
        row = {
            "to": to, "amount_usdc": amount, "memo": memo,
            "kind": kind, "ts": float(self._clock()),
        }
        self.ledger.append({"debit": row})
        out = {"tx_hash": tx_hash, **row}
        if confirmation is not None:
            out["confirmation"] = confirmation
        return out

    def _confirm_if_required(self, tx_hash: str) -> dict | None:
        """Block until ``tx_hash`` confirms when mainnet-confirmation is
        enabled and the chain client supports it. Returns the receipt
        dict on success; raises :class:`WalletError` on timeout/failure.
        Returns ``None`` when no waiting was requested or the chain
        client doesn't honour the :class:`ConfirmingChainClient`
        shape — silent no-op preserves backwards-compat with stubs."""
        if not self._wait_for_confirmation or not tx_hash:
            return None
        waiter = getattr(self._chain, "wait_for_confirmation", None)
        if not callable(waiter):
            return None
        try:
            receipt = waiter(
                tx_hash,
                timeout_s=self._confirmation_timeout_s,
                min_confirmations=self._min_confirmations,
            )
        except Exception as exc:
            raise WalletError(
                f"on-chain confirmation failed for {tx_hash}: {exc}"
            ) from exc
        if not isinstance(receipt, dict):
            return {"raw": str(receipt)}
        return receipt

    # -- credit (incoming revenue) ----------------------------------------
    def credit(self, amount_usdc: float, source: str) -> None:
        """Real revenue lands on-chain (incoming USDC transfer to the
        adapter's address). The platform calls this when it observes
        such a transfer — we record locally so the genome's perceive
        node sees the movement on the next heartbeat. The chain's
        balance independently reflects the credit; reconciliation
        catches mismatches."""
        amount = float(amount_usdc)
        if amount < 0:
            raise WalletError("negative credit")
        self._last_revenue_ts = float(self._clock())
        self.ledger.append(
            {"credit": {"from": source, "amount_usdc": amount,
                        "ts": float(self._clock())}}
        )

    # -- harvest (owner withdrawal) ---------------------------------------
    def harvest_to_owner(
        self, *, destination: str, net_usdc: float, fee_usdc: float,
    ) -> dict:
        net = float(net_usdc)
        fee = float(fee_usdc)
        if net < 0 or fee < 0:
            raise WalletError("harvest amounts must be non-negative")
        gross = net + fee
        if gross <= 0:
            raise WalletError("harvest amount is zero")
        if gross > self._chain.balance_usdc() + 1e-9:
            raise WalletError(
                f"harvest of ${gross:.2f} exceeds on-chain balance"
            )
        # Screen the owner's destination — a harvest is the largest single
        # outbound the platform ever issues, so this is the highest-value
        # screening call we make. Platform fee address bypasses (symbolic).
        if self._sanctions is not None:
            verdict = self._sanctions.check(str(destination))
            if not verdict.allowed:
                raise WalletError(
                    f"sanctions:{verdict.source} refused harvest "
                    f"destination {destination!r}: {verdict.reason or 'refused'}"
                )
        ts = float(self._clock())
        # Two real transfers: one to the owner, one to the platform's
        # fee address. A real implementation would batch via a multisend
        # or a small contract to make these atomic; here we issue them
        # sequentially and accept the (small) window between them.
        try:
            if net > 0:
                self._chain.transfer(
                    to=destination, amount_usdc=net, memo="harvest:owner",
                )
            if fee > 0:
                self._chain.transfer(
                    to="platform.fee",  # ChainClient may resolve symbolically
                    amount_usdc=fee, memo="harvest:platform_fee",
                )
        except Exception as exc:
            raise WalletError(f"harvest chain transfer failed: {exc}") from exc
        if net > 0:
            self.ledger.append({"harvest": {
                "destination": destination, "amount_usdc": net,
                "kind": "owner_net", "ts": ts,
            }})
        if fee > 0:
            self.ledger.append({"harvest": {
                "destination": "platform.fee", "amount_usdc": fee,
                "kind": "platform_fee", "ts": ts,
            }})
        return {
            "gross_usdc": gross, "net_usdc": net, "fee_usdc": fee,
            "destination": destination, "ts": ts,
        }

    # -- platform-installed policy ----------------------------------------
    def set_policy(
        self,
        *,
        per_action_cap_usdc: float | None = None,
        per_day_cap_usdc: float | None = None,
        per_lifetime_cap_usdc: float | None = None,
    ) -> dict:
        if per_action_cap_usdc is not None:
            if per_action_cap_usdc < 0:
                raise WalletError("per_action_cap_usdc must be non-negative")
            self._per_action_cap = min(
                float(per_action_cap_usdc), HARD_CAPS.per_action_usdc
            )
        if per_day_cap_usdc is not None:
            if per_day_cap_usdc < 0:
                raise WalletError("per_day_cap_usdc must be non-negative")
            self._per_day_cap = min(
                float(per_day_cap_usdc), HARD_CAPS.per_day_usdc
            )
        if per_lifetime_cap_usdc is not None:
            if per_lifetime_cap_usdc < 0:
                raise WalletError("per_lifetime_cap_usdc must be non-negative")
            self._per_lifetime_cap = min(
                float(per_lifetime_cap_usdc), HARD_CAPS.per_lifetime_usdc
            )
        return self.policy()

    def policy(self) -> dict:
        return {
            "per_action_cap_usdc": min(
                HARD_CAPS.per_action_usdc, self._per_action_cap
            ),
            "per_day_cap_usdc": min(
                HARD_CAPS.per_day_usdc, self._per_day_cap
            ),
            "per_lifetime_cap_usdc": min(
                HARD_CAPS.per_lifetime_usdc, self._per_lifetime_cap
            ),
            "hard_per_action_usdc": HARD_CAPS.per_action_usdc,
            "hard_per_day_usdc": HARD_CAPS.per_day_usdc,
            "hard_per_lifetime_usdc": HARD_CAPS.per_lifetime_usdc,
        }

    def owner_soft_caps(self) -> OwnerSoftCaps | None:
        if (
            self._per_action_cap >= HARD_CAPS.per_action_usdc
            and self._per_day_cap >= HARD_CAPS.per_day_usdc
            and self._per_lifetime_cap >= HARD_CAPS.per_lifetime_usdc
        ):
            return None
        return OwnerSoftCaps(
            per_action_usdc=(
                self._per_action_cap
                if self._per_action_cap < HARD_CAPS.per_action_usdc
                else None
            ),
            per_day_usdc=(
                self._per_day_cap
                if self._per_day_cap < HARD_CAPS.per_day_usdc
                else None
            ),
            per_lifetime_usdc=(
                self._per_lifetime_cap
                if self._per_lifetime_cap < HARD_CAPS.per_lifetime_usdc
                else None
            ),
        )

    # -- owner pre-approvals (doc 17 §"Darwin paying a human") -------------
    def set_preapproval(self, rule: PreApprovalRule) -> PreApprovalRule:
        """Install or replace a pre-approval rule keyed on ``rule.rule_id``.
        Same shape as :class:`FakeWallet.set_preapproval` so the supervisor
        wires both wallets identically."""
        if not isinstance(rule, PreApprovalRule):
            raise WalletError("set_preapproval requires a PreApprovalRule")
        self._preapprovals[rule.rule_id] = rule
        return rule

    def revoke_preapproval(self, rule_id: str) -> bool:
        return self._preapprovals.pop(rule_id, None) is not None

    def owner_preapprovals(self) -> tuple[PreApprovalRule, ...]:
        return tuple(
            self._preapprovals[k] for k in sorted(self._preapprovals)
        )

    def consume_preapproval(self, rule_id: str) -> PreApprovalRule | None:
        """See :meth:`FakeWallet.consume_preapproval`. Used-count is
        platform-side accounting; the chain doesn't track it."""
        existing = self._preapprovals.get(rule_id)
        if existing is None:
            return None
        bumped = existing.model_copy(
            update={"used_count": existing.used_count + 1},
        )
        self._preapprovals[rule_id] = bumped
        if self._preapproval_persister is not None:
            try:
                self._preapproval_persister(bumped)
            except Exception:
                pass
        return bumped

    def set_preapproval_persister(self, callback) -> None:
        self._preapproval_persister = callback

    # -- reproduction transfer --------------------------------------------
    def set_sanctions_screener(self, screener) -> None:
        """Install (or replace) the screener at runtime. Used by the
        Supervisor at spawn time so a single fleet-wide screener reaches
        every Darwin's wallet."""
        self._sanctions = screener

    def transfer_out_for_child(
        self, amount_usdc: float, *, destination: str = "platform.omnibus",
    ) -> None:
        """The platform's omnibus move of the parent's surplus on
        reproduction. Submits a USDC transfer to ``destination`` — the
        platform's omnibus wallet address (the supervisor injects it when
        an :class:`OmnibusWallet` is wired) or a symbolic fallback used
        only by unit tests that don't care where the bytes land.

        The omnibus's :meth:`OmnibusWallet.fund_child` is the second leg
        that moves the funds from omnibus → child's address, attributed
        to the owning user. That call lives in the supervisor's spawn
        observation flow, not here."""
        amount = float(amount_usdc)
        if amount > self._chain.balance_usdc() + 1e-9:
            raise WalletError("reproduction budget exceeds on-chain balance")
        try:
            self._chain.transfer(
                to=destination,
                amount_usdc=amount,
                memo="reproduction:omnibus_transfer",
            )
        except Exception as exc:
            raise WalletError(
                f"reproduction transfer failed: {exc}"
            ) from exc
        self.ledger.append(
            {"reproduction_transfer": {
                "amount_usdc": amount,
                "destination": destination,
                "ts": float(self._clock()),
            }}
        )

    def transfer_out_for_fiat_payout(
        self, amount_usdc: float, *, destination: str = "platform.omnibus",
    ) -> None:
        """First leg of the Darwin → fiat payout flow. Submits a USDC
        transfer to the omnibus address so the platform can convert
        USDC → fiat and ship the payout via the configured
        :class:`FiatRamp`. The :meth:`OmnibusWallet.relay_fiat_payout`
        second leg performs the fiat-side withdraw and records the
        payout in the omnibus ledger."""
        amount = float(amount_usdc)
        if amount > self._chain.balance_usdc() + 1e-9:
            raise WalletError("fiat payout exceeds on-chain balance")
        try:
            self._chain.transfer(
                to=destination,
                amount_usdc=amount,
                memo="fiat_payout:omnibus_transfer",
            )
        except Exception as exc:
            raise WalletError(
                f"fiat payout transfer failed: {exc}"
            ) from exc
        self.ledger.append(
            {"fiat_payout_transfer": {
                "amount_usdc": amount,
                "destination": destination,
                "ts": float(self._clock()),
            }}
        )

    def sweep_to(
        self, *, destination: str, amount_usdc: float,
    ) -> dict:
        """Platform-driven cleanup move (e.g. dead-Darwin residual back
        to the omnibus address). Bypasses the per-action / per-day /
        lifetime caps because it's a platform sweep, not a Darwin
        spend. Submits a real chain transfer and records the tx in the
        local ledger. Returns a ledger-shaped row including the tx hash."""
        amount = float(amount_usdc)
        if amount < 0:
            raise WalletError("sweep amount must be non-negative")
        if amount == 0:
            return {"amount_usdc": 0.0, "destination": destination,
                    "ts": float(self._clock()), "tx_hash": ""}
        if not destination:
            raise WalletError("sweep requires a destination")
        if amount > self._chain.balance_usdc() + 1e-9:
            raise WalletError(
                f"sweep amount ${amount:.2f} exceeds on-chain balance"
            )
        try:
            tx = self._chain.transfer(
                to=destination, amount_usdc=amount,
                memo="sweep:platform",
            )
        except Exception as exc:
            raise WalletError(f"sweep transfer failed: {exc}") from exc
        tx_hash = tx.get("tx_hash", "") if isinstance(tx, dict) else ""
        row = {
            "amount_usdc": amount,
            "destination": destination,
            "ts": float(self._clock()),
            "tx_hash": str(tx_hash),
        }
        self.ledger.append({"sweep_out": row})
        return row
