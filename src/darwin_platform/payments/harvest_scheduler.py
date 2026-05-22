"""Recurring profit-harvest rules (docs/engineering/05).

Doc 05 specifies the harvest rule shape: "move balance above $X to external
wallet on schedule Y". The :class:`darwin_platform.supervisor.Supervisor`
already exposes a one-shot ``harvest()`` for operator-driven withdrawals;
this module adds the **scheduled** half — rules the owner registers once
that the supervisor evaluates at every drive cycle.

Architecture:

- :class:`HarvestRule` — one rule. ``trigger_balance_usdc`` is the floor
  the wallet must clear before a harvest is even considered;
  ``amount_usdc`` is the harvest size (or None for "all above the
  trigger"); ``interval_seconds`` is the minimum gap between executions.
- :class:`HarvestScheduler` — holds the rules + evaluates them. The
  supervisor's drive cycle calls :meth:`due` with a per-Darwin balance
  snapshot, gets back a list of rules ready to run, and executes them.
- Per-Darwin scoping: each rule has a ``darwin_id`` and only applies to
  that Darwin's wallet.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass
from typing import Callable


# Default rules sleep at least an hour between fires so a flapping wallet
# balance doesn't drain into the harvest fee endlessly.
DEFAULT_INTERVAL_SECONDS: float = 24 * 3600.0


class HarvestRuleError(Exception):
    """Rule validation / scheduling refusal."""


@dataclass(frozen=True)
class HarvestRule:
    """One scheduled harvest rule. Frozen so a stored rule can't drift; an
    update is a delete-+-recreate (which produces an audit pair)."""

    id: str
    darwin_id: str
    destination: str
    trigger_balance_usdc: float
    amount_usdc: float | None        # None = harvest everything above trigger
    interval_seconds: float
    enabled: bool
    created_at: float
    last_run_at: float | None
    last_amount_usdc: float | None

    def to_json(self) -> dict:
        return asdict(self)

    def with_run(self, *, at: float, amount: float) -> "HarvestRule":
        return HarvestRule(
            id=self.id, darwin_id=self.darwin_id, destination=self.destination,
            trigger_balance_usdc=self.trigger_balance_usdc,
            amount_usdc=self.amount_usdc,
            interval_seconds=self.interval_seconds,
            enabled=self.enabled, created_at=self.created_at,
            last_run_at=at, last_amount_usdc=amount,
        )

    def with_enabled(self, enabled: bool) -> "HarvestRule":
        return HarvestRule(
            id=self.id, darwin_id=self.darwin_id, destination=self.destination,
            trigger_balance_usdc=self.trigger_balance_usdc,
            amount_usdc=self.amount_usdc,
            interval_seconds=self.interval_seconds,
            enabled=bool(enabled), created_at=self.created_at,
            last_run_at=self.last_run_at, last_amount_usdc=self.last_amount_usdc,
        )


@dataclass(frozen=True)
class HarvestDue:
    """One rule ready to fire, paired with the amount to harvest."""

    rule: HarvestRule
    amount_usdc: float


class HarvestScheduler:
    """Holds rules + evaluates them. The supervisor owns one instance and
    calls :meth:`due` per heartbeat with a per-Darwin balance probe."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._rules: dict[str, HarvestRule] = {}

    # -- rule CRUD -------------------------------------------------------
    def add(
        self,
        *,
        darwin_id: str,
        destination: str,
        trigger_balance_usdc: float,
        amount_usdc: float | None = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ) -> HarvestRule:
        if not destination:
            raise HarvestRuleError("destination is required")
        if trigger_balance_usdc <= 0:
            raise HarvestRuleError("trigger_balance_usdc must be > 0")
        if amount_usdc is not None and amount_usdc <= 0:
            raise HarvestRuleError("amount_usdc must be > 0 or null")
        if interval_seconds < 60:
            raise HarvestRuleError(
                "interval_seconds must be >= 60 (one minute floor)"
            )
        rule = HarvestRule(
            id=uuid.uuid4().hex,
            darwin_id=darwin_id,
            destination=destination,
            trigger_balance_usdc=float(trigger_balance_usdc),
            amount_usdc=float(amount_usdc) if amount_usdc is not None else None,
            interval_seconds=float(interval_seconds),
            enabled=True,
            created_at=self._clock(),
            last_run_at=None,
            last_amount_usdc=None,
        )
        self._rules[rule.id] = rule
        return rule

    def remove(self, rule_id: str) -> HarvestRule:
        rule = self._rules.pop(rule_id, None)
        if rule is None:
            raise HarvestRuleError(f"unknown rule {rule_id!r}")
        return rule

    def list(self, darwin_id: str | None = None) -> list[HarvestRule]:
        if darwin_id is None:
            return list(self._rules.values())
        return [r for r in self._rules.values() if r.darwin_id == darwin_id]

    def set_enabled(self, rule_id: str, enabled: bool) -> HarvestRule:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise HarvestRuleError(f"unknown rule {rule_id!r}")
        updated = rule.with_enabled(enabled)
        self._rules[rule_id] = updated
        return updated

    # -- evaluation ------------------------------------------------------
    def due(
        self,
        *,
        darwin_id: str,
        balance_usdc: float,
    ) -> list[HarvestDue]:
        """Return the rules for this Darwin that are ready to fire NOW.
        The caller is responsible for executing them via the wallet."""
        now = self._clock()
        out: list[HarvestDue] = []
        for rule in self._rules.values():
            if rule.darwin_id != darwin_id or not rule.enabled:
                continue
            if (
                rule.last_run_at is not None
                and (now - rule.last_run_at) < rule.interval_seconds
            ):
                continue
            if balance_usdc <= rule.trigger_balance_usdc:
                continue
            if rule.amount_usdc is None:
                # "Everything above the trigger" — clamp to balance.
                amount = balance_usdc - rule.trigger_balance_usdc
            else:
                # Fixed-amount rule — only fire if the wallet has room
                # both for the amount AND the trigger floor.
                if balance_usdc - rule.amount_usdc < rule.trigger_balance_usdc:
                    continue
                amount = rule.amount_usdc
            if amount <= 0:
                continue
            out.append(HarvestDue(rule=rule, amount_usdc=amount))
        return out

    def mark_run(
        self, rule_id: str, *, amount: float, at: float | None = None,
    ) -> HarvestRule:
        """Record that ``rule_id`` ran at ``at`` for ``amount`` USDC. The
        caller calls this AFTER the supervisor.harvest() returns ok."""
        rule = self._rules.get(rule_id)
        if rule is None:
            raise HarvestRuleError(f"unknown rule {rule_id!r}")
        updated = rule.with_run(at=at if at is not None else self._clock(),
                                amount=amount)
        self._rules[rule_id] = updated
        return updated
