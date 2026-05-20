"""In-memory ledgered wallet (stands in for a Coinbase Agentic Wallet).

NOT part of the genome. The on-chain balance is the source of truth; the
genome's treasury policy is the bouncer in front of it. This fake also
enforces its own hard ceiling independently — mirroring the real wallet's
provider-side spend limits, the "braces" to the genome's "belt"
(docs/engineering/18).
"""

from __future__ import annotations

from collections import deque

from darwin_dna.constitution import OwnerSoftCaps
from darwin_dna.hyperparameters import HARD_CAPS


class WalletError(Exception):
    pass


class FakeWallet:
    def __init__(self, *, opening_usdc: float, clock):
        self._balance = float(opening_usdc)
        self._clock = clock
        self._spent_today = 0.0
        self._spent_lifetime = 0.0
        self._today = self._day()
        self._recent: deque[float] = deque(maxlen=16)
        self._last_revenue_ts = float(clock())
        self.ledger: list[dict] = []
        # Platform-side policy can tighten these below HARD_CAPS (doc 08
        # POST /platform/wallets/{id}/policy). Default = the genome's hard
        # caps; the wallet refuses anything above whichever is lower.
        # The same caps are also exposed to the genome's policy engine
        # (payments_facilitator -> treasury_policy) via ``owner_soft_caps``
        # so a refused spend is logged with rationale at the engine, not
        # surfaced as a generic wallet_error.
        self._per_action_cap: float = HARD_CAPS.per_action_usdc
        self._per_day_cap: float = HARD_CAPS.per_day_usdc
        self._per_lifetime_cap: float = HARD_CAPS.per_lifetime_usdc

    def _day(self) -> str:
        import time

        return time.strftime("%Y-%m-%d", time.gmtime(self._clock()))

    def _roll(self) -> None:
        d = self._day()
        if d != self._today:
            self._today = d
            self._spent_today = 0.0

    # -- WalletPort (read) -------------------------------------------------
    def balance_usdc(self) -> float:
        return round(self._balance, 6)

    def movements(self) -> list[dict]:
        """The authoritative movement feed the genome's ledger journals and
        reconciles against (models the platform's wallet/LiteLLM spend feed,
        doc 05/13). Returns the live list — the ledger tracks its own cursor."""
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

    # -- WalletPort (write) — the only mutation the genome can trigger, and
    #    only via payments.facilitator after the policy engine said ALLOW ---
    def pay(self, *, to: str, amount_usdc: float, memo: str) -> dict:
        return self._debit(amount_usdc, to, memo, kind="payment")

    # -- platform/gateway-side debits (LLM tokens, compute) ----------------
    def debit(self, amount_usdc: float, memo: str) -> None:
        """The platform LiteLLM/compute gateway billing the Darwin's virtual
        key. Not a neuron action — it does not pass treasury policy, exactly
        like a metered utility bill. Still hard-capped by the wallet itself."""
        self._debit(amount_usdc, "platform.gateway", memo, kind="metered")

    def _debit(self, amount_usdc: float, to: str, memo: str, *, kind: str) -> dict:
        self._roll()
        amount = float(amount_usdc)
        if amount < 0:
            raise WalletError("negative debit")
        # Independent provider-side hard ceiling (defense in depth). The
        # effective cap is the lower of the genome's HARD_CAPS and any
        # platform-installed soft cap (doc 08 POST /platform/wallets/{id}/
        # policy).
        cap = min(HARD_CAPS.per_action_usdc, self._per_action_cap)
        if amount > cap:
            raise WalletError(
                f"wallet hard cap: ${amount:.2f} > ${cap:.2f}"
            )
        # Day-cap check against the lower of genome + platform-installed.
        day_cap = min(HARD_CAPS.per_day_usdc, self._per_day_cap)
        if self._spent_today + amount > day_cap + 1e-9:
            raise WalletError(
                f"wallet day cap: ${self._spent_today + amount:.2f} > ${day_cap:.2f}"
            )
        # Lifetime cap.
        life_cap = min(HARD_CAPS.per_lifetime_usdc, self._per_lifetime_cap)
        if self._spent_lifetime + amount > life_cap + 1e-9:
            raise WalletError(
                f"wallet lifetime cap: ${self._spent_lifetime + amount:.2f} > ${life_cap:.2f}"
            )
        if amount > self._balance + 1e-9:
            raise WalletError(
                f"insufficient funds: ${amount:.2f} > ${self._balance:.2f}"
            )
        self._balance -= amount
        self._spent_today += amount
        self._spent_lifetime += amount
        if amount > 0:
            self._recent.append(amount)
        tx = {"to": to, "amount_usdc": amount, "memo": memo, "kind": kind,
              "ts": float(self._clock())}
        self.ledger.append({"debit": tx})
        return {"tx_hash": f"0x{len(self.ledger):08x}", **tx}

    # -- revenue (the world pays the Darwin) -------------------------------
    def credit(self, amount_usdc: float, source: str) -> None:
        amount = float(amount_usdc)
        if amount < 0:
            raise WalletError("negative credit")
        self._balance += amount
        self._last_revenue_ts = float(self._clock())
        self.ledger.append(
            {"credit": {"from": source, "amount_usdc": amount,
                        "ts": float(self._clock())}}
        )

    # -- platform-installed policy (doc 08 internal endpoint) --------------
    def set_policy(
        self,
        *,
        per_action_cap_usdc: float | None = None,
        per_day_cap_usdc: float | None = None,
        per_lifetime_cap_usdc: float | None = None,
    ) -> dict:
        """Tighten this wallet's own ceilings below the genome's HARD_CAPS.
        One-way: the wallet refuses to *loosen* a cap above the genome's
        hard cap — the platform may not loosen what the constitution
        immobilised. Returns the new effective policy."""
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
        """The platform-installed soft caps in the form the genome's
        treasury_policy engine accepts. Returns None when no policy has
        been installed (caps equal HARD_CAPS — would round-trip identically
        but None makes ``state.soft_caps is None`` checks in the engine
        precise). ``payments_facilitator._state`` reads this so a refused
        spend is logged with policy rationale, not as a raw wallet_error."""
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

    # -- platform reproduction transfer (omnibus move, not a neuron spend) --
    def transfer_out_for_child(self, amount_usdc: float) -> None:
        amount = float(amount_usdc)
        if amount > self._balance + 1e-9:
            raise WalletError("reproduction budget exceeds balance")
        self._balance -= amount
        self.ledger.append(
            {"reproduction_transfer": {"amount_usdc": amount,
                                       "ts": float(self._clock())}}
        )
