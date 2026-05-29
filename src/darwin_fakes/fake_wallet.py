"""In-memory ledgered wallet (stands in for a Coinbase Agentic Wallet).

NOT part of the genome. The on-chain balance is the source of truth; the
genome's treasury policy is the bouncer in front of it. This fake also
enforces its own hard ceiling independently — mirroring the real wallet's
provider-side spend limits, the "braces" to the genome's "belt"
(docs/engineering/18).
"""

from __future__ import annotations

from collections import deque

from darwin_dna.constitution import OwnerSoftCaps, PreApprovalRule
from darwin_dna.hyperparameters import HARD_CAPS


class WalletError(Exception):
    pass


class FakeWallet:
    def __init__(self, *, opening_usdc: float, clock):
        self._balance = float(opening_usdc)
        self._clock = clock
        # Synthetic, stable per-instance on-chain address. The 90-day sim
        # and unit tests don't talk to a real chain; the supervisor's
        # reproduction-relay helper still needs each Darwin's wallet to
        # surface SOMETHING address-shaped so the parent → omnibus → child
        # second leg can route to a distinct destination per child.
        # Real wallets (RealWallet via ChainClient) override this with the
        # actual on-chain address.
        import secrets
        self._address = f"fake-wallet-{secrets.token_hex(8)}"
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
        # Owner spend-lock (doc 09 owner controls). When engaged the wallet
        # refuses every *outbound payment* — a neuron paying a counterparty —
        # while still allowing metered LLM/compute billing, so the Darwin
        # keeps running. Distinct from a $0 cap: releasing the lock restores
        # whatever caps the owner set, untouched.
        self._spend_locked: bool = False
        self._spend_lock_reason: str | None = None
        # Optional sanctions screener (doc 17). Default None preserves
        # legacy behaviour (the 90-day sim's recipients are all symbolic
        # like "blog.example/affiliate"). The platform installs one at
        # spawn time via :meth:`set_sanctions_screener`.
        self._sanctions = None
        # Owner pre-approval rules (doc 17 §"What about a Darwin paying a
        # human?"). Keyed by rule_id so the platform can revoke a single
        # rule without disturbing the others. The genome's facilitator
        # reads ``owner_preapprovals()`` and forwards them to the policy
        # engine; the engine's escalation layer treats a matching rule as
        # ALLOW with rationale rather than ESCALATE.
        self._preapprovals: dict[str, PreApprovalRule] = {}
        # Optional persistence write-through. The supervisor installs a
        # callback on spawn so consume_preapproval bumps survive a
        # restart. Default None → in-memory only.
        self._preapproval_persister = None

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

    def address(self) -> str:
        """Synthetic per-instance address; see __init__ for rationale."""
        return self._address

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
        # Owner spend-lock (doc 09): refuse outbound payments while engaged.
        # Metered platform billing (LLM/compute) is exempt — locking spend
        # stops the Darwin moving money, it does not stop it thinking.
        if kind == "payment" and self._spend_locked:
            raise WalletError(
                f"spend locked: "
                f"{self._spend_lock_reason or 'owner spend-lock engaged'}"
            )
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
        if self._sanctions is not None:
            verdict = self._sanctions.check(str(to))
            if not verdict.allowed:
                raise WalletError(
                    f"sanctions:{verdict.source} refused recipient "
                    f"{to!r}: {verdict.reason or 'screening refused'}"
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
            "spend_locked": self._spend_locked,
            "spend_lock_reason": self._spend_lock_reason,
        }

    # -- owner spend-lock (doc 09 owner controls) --------------------------
    def set_spend_lock(
        self, locked: bool, *, reason: str | None = None
    ) -> dict:
        """Engage or release the owner spend-lock. While locked the wallet
        refuses every outbound payment; metered LLM/compute billing still
        goes through so the Darwin keeps running. Returns the lock state."""
        self._spend_locked = bool(locked)
        self._spend_lock_reason = reason if self._spend_locked else None
        return self.spend_lock()

    def spend_lock(self) -> dict:
        return {
            "spend_locked": self._spend_locked,
            "reason": self._spend_lock_reason,
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

    # -- owner pre-approvals (doc 17 §"Darwin paying a human") -------------
    def set_preapproval(self, rule: PreApprovalRule) -> PreApprovalRule:
        """Install or replace a pre-approval rule keyed on ``rule.rule_id``.
        The platform owns the rule lifecycle (creation + revocation); the
        wallet just holds the live set so the facilitator can read it."""
        if not isinstance(rule, PreApprovalRule):
            raise WalletError("set_preapproval requires a PreApprovalRule")
        self._preapprovals[rule.rule_id] = rule
        return rule

    def revoke_preapproval(self, rule_id: str) -> bool:
        """Drop a rule by id. Returns True if a rule was removed, False
        if no such rule existed (idempotent — re-revoking is a no-op)."""
        return self._preapprovals.pop(rule_id, None) is not None

    def owner_preapprovals(self) -> tuple[PreApprovalRule, ...]:
        """The live set in deterministic order (sorted by rule_id) so the
        facilitator's match scan is reproducible across heartbeats."""
        return tuple(
            self._preapprovals[k] for k in sorted(self._preapprovals)
        )

    def consume_preapproval(self, rule_id: str) -> PreApprovalRule | None:
        """Bump the used-counter on a rule and return the updated copy.
        Called by the facilitator after a successful pay that the rule
        cleared. The rule is frozen (Pydantic), so we replace it with a
        new instance carrying ``used_count + 1``. Returns None when no
        such rule exists (the facilitator treats that as a no-op — the
        spend already succeeded, the counter just couldn't advance).

        Calls the optional persistence write-through callback so the
        consume survives a restart. The callback is set by the
        supervisor at spawn time when persistence is configured."""
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
        """The supervisor calls this on spawn to wire the wallet's
        consume_preapproval bumps through to the persistent store."""
        self._preapproval_persister = callback

    # -- harvest to owner (doc 05, doc 08 §"Billing model") ---------------
    def harvest_to_owner(
        self,
        *,
        destination: str,
        net_usdc: float,
        fee_usdc: float,
    ) -> dict:
        """Move ``net_usdc`` to the owner's external address and ``fee_usdc``
        to the platform's fee account. One on-chain operation per part in
        production; here both are recorded as movements on the wallet's
        ledger with a ``harvest`` kind so the platform's billing service can
        attribute them. Refuses when balance is short or amounts are bad.
        Does NOT pass through the genome's policy engine — harvest is a
        platform-mediated transaction; the policy engine guards *neuron*
        spends."""
        net = float(net_usdc)
        fee = float(fee_usdc)
        if net < 0 or fee < 0:
            raise WalletError("harvest amounts must be non-negative")
        gross = net + fee
        if gross <= 0:
            raise WalletError("harvest amount is zero")
        if gross > self._balance + 1e-9:
            raise WalletError(
                f"harvest of ${gross:.2f} exceeds balance ${self._balance:.2f}"
            )
        if self._sanctions is not None:
            verdict = self._sanctions.check(str(destination))
            if not verdict.allowed:
                raise WalletError(
                    f"sanctions:{verdict.source} refused harvest destination "
                    f"{destination!r}: {verdict.reason or 'refused'}"
                )
        self._balance -= gross
        ts = float(self._clock())
        net_row = {
            "harvest": {
                "destination": destination,
                "amount_usdc": net,
                "kind": "owner_net",
                "ts": ts,
            }
        }
        fee_row = {
            "harvest": {
                "destination": "platform.fee",
                "amount_usdc": fee,
                "kind": "platform_fee",
                "ts": ts,
            }
        }
        # Net first, fee second — operator-visible chronological order.
        if net > 0:
            self.ledger.append(net_row)
        if fee > 0:
            self.ledger.append(fee_row)
        return {
            "gross_usdc": gross,
            "net_usdc": net,
            "fee_usdc": fee,
            "destination": destination,
            "ts": ts,
        }

    # -- runtime-installable sanctions screener (doc 17) -------------------
    def set_sanctions_screener(self, screener) -> None:
        """Install (or replace) the screener at runtime. Used by the
        Supervisor at spawn time so a single fleet-wide screener reaches
        every Darwin's wallet without per-test wiring."""
        self._sanctions = screener

    # -- platform reproduction transfer (omnibus move, not a neuron spend) --
    def transfer_out_for_child(
        self, amount_usdc: float, *, destination: str = "platform.omnibus",
    ) -> None:
        """Move the reproduction budget from the parent's wallet toward
        ``destination`` (the platform's omnibus address when wired; symbolic
        otherwise). The omnibus.fund_child second leg credits the child's
        per-Darwin address — that's the supervisor's responsibility, not
        the wallet's."""
        amount = float(amount_usdc)
        if amount > self._balance + 1e-9:
            raise WalletError("reproduction budget exceeds balance")
        self._balance -= amount
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
        """First leg of the Darwin → fiat payout flow: debit the Darwin's
        USDC wallet to the omnibus address so the platform can subsequently
        convert that USDC to fiat via the configured FiatRamp. The
        omnibus.relay_fiat_payout second leg pushes fiat to the verified
        destination — supervisor responsibility, not the wallet's."""
        amount = float(amount_usdc)
        if amount > self._balance + 1e-9:
            raise WalletError("fiat payout exceeds balance")
        self._balance -= amount
        self.ledger.append(
            {"fiat_payout_transfer": {
                "amount_usdc": amount,
                "destination": destination,
                "ts": float(self._clock()),
            }}
        )
