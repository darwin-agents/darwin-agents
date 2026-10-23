"""The per-Darwin double-entry ledger (docs/engineering/05).

Every money movement is a double-entry, hash-chained journal entry. The
ledger is the source of truth for the dashboard, fitness (profit), tax, and
audit — but **not** for the balance itself. The on-chain wallet is
authoritative for the balance; the ledger is *reconciled against the wallet
every heartbeat*, and a drift larger than the configured tolerance halts the
Darwin (doc 05). That reconciliation lived only in the external sim harness
(invariant I7) before; now the genome enforces it on itself.

Accounting convention: treasury is an *asset* account, which is debit-normal
(assets increase on the debit side):

    treasury_balance = Σ amount where debit  == treasury
                      − Σ amount where credit == treasury

so opening capital debits treasury (asset up) and credits an external funding
source; an expense debits the expense account and credits treasury (asset
down); revenue debits treasury (asset up) and credits a revenue account; a
reproduction transfer debits equity and credits treasury (asset down).
"""

from __future__ import annotations

import hashlib
from collections import deque
from typing import Any

from pydantic import BaseModel, Field

from .._canonical import canonical_bytes

GENESIS_HASH = "0" * 64

# How many recent entries the in-memory window retains. The ledger is a
# *derived* structure (the wallet movement feed + audit chain are the durable
# record), and balance is an O(1) running total — so an immortal genome must
# not pin every entry it ever posts in RAM. The retained window is what
# ``verify()``/``entries`` inspect; it is generous enough that any sim or test
# sees its whole short history, while a long-lived pod stays flat. Tunable per
# instance for tests that want to assert windowing directly.
DEFAULT_LEDGER_WINDOW = 10_000
TREASURY = "treasury"
FUNDING_EXTERNAL = "funding:external"
EQUITY_REPRODUCTION = "equity:reproduction"
EQUITY_OWNER_DRAW = "equity:owner_draw"
# A credit whose source starts with this prefix is new capital (owner
# deposit, on-ramp), not revenue: it funds the treasury and never counts as
# fitness profit.
DEPOSIT_PREFIX = "deposit:"


class LedgerError(Exception):
    """The ledger is internally inconsistent or has drifted from the
    authoritative wallet beyond tolerance. The Darwin must halt (doc 05)."""


def movement_leg(mv: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Split one wallet movement row ``{kind: payload}`` into its parts.
    Every wallet emits single-key rows; anything else is a feed we cannot
    account for, which must halt the Darwin like any other drift."""
    if not isinstance(mv, dict) or len(mv) != 1:
        raise LedgerError(f"malformed wallet movement: {mv!r}")
    ((kind, payload),) = mv.items()
    if not isinstance(payload, dict):
        raise LedgerError(f"malformed wallet movement payload: {mv!r}")
    return str(kind), payload


def is_deposit(mv: dict[str, Any]) -> bool:
    """True for a capital inflow (see :data:`DEPOSIT_PREFIX`)."""
    kind, m = movement_leg(mv)
    return kind == "credit" and str(m.get("from", "")).startswith(DEPOSIT_PREFIX)


def treasury_delta(mv: dict[str, Any]) -> float:
    """Signed effect of one movement on the treasury (inflow positive)."""
    debit, credit, amount, _cat, _cp = Ledger._classify(mv)
    return amount if debit == TREASURY else -amount


class LedgerEntry(BaseModel):
    model_config = {"frozen": True}

    seq: int
    ts: float
    debit: str
    credit: str
    amount_usdc: float = Field(gt=0)
    category: str
    counterparty: str | None = None
    action_id: str | None = None  # links to the audit event log entry
    prev_hash: str
    hash: str


def _hash(seq, prev, ts, debit, credit, amount, category, counterparty, action_id) -> str:
    return hashlib.sha256(
        canonical_bytes(
            {
                "seq": seq,
                "prev_hash": prev,
                "ts": ts,
                "debit": debit,
                "credit": credit,
                "amount_usdc": amount,
                "category": category,
                "counterparty": counterparty,
                "action_id": action_id,
            }
        )
    ).hexdigest()


class Ledger:
    """Append-only, hash-chained, double-entry. In production this is a
    Postgres table; here it is an in-memory list rebuilt from the wallet's
    authoritative movement feed at boot (the wallet/audit are durable; the
    ledger is derived and reconciled, never the source of truth for balance)."""

    def __init__(self, darwin_id: str, *, window: int = DEFAULT_LEDGER_WINDOW):
        self._darwin_id = darwin_id
        # Bounded ring of the most-recent entries. The full chain is derived
        # (replayable from the wallet feed) and the durable record is the
        # audit log + wallet; pinning every entry forever is the pod leak.
        self._entries: deque[LedgerEntry] = deque(maxlen=max(1, int(window)))
        self._seq = 0
        self._head = GENESIS_HASH
        self._seeded = False
        self._cursor = 0  # how many wallet movements have been journaled
        # O(1) running treasury balance, updated on every post — so
        # ``treasury_balance()`` stays exact even after old entries fall out
        # of the window (it was an O(n)-per-heartbeat re-sum before).
        self._treasury = 0.0

    # -- posting -----------------------------------------------------------
    def _post(
        self, *, debit: str, credit: str, amount: float, category: str,
        ts: float, counterparty: str | None = None, action_id: str | None = None,
    ) -> LedgerEntry:
        if debit == credit:
            raise LedgerError(f"degenerate entry: {debit} == {credit}")
        if amount <= 0:
            raise LedgerError(f"non-positive ledger amount {amount}")
        h = _hash(self._seq, self._head, ts, debit, credit, amount, category,
                  counterparty, action_id)
        e = LedgerEntry(
            seq=self._seq, ts=ts, debit=debit, credit=credit,
            amount_usdc=amount, category=category, counterparty=counterparty,
            action_id=action_id, prev_hash=self._head, hash=h,
        )
        self._entries.append(e)
        if debit == TREASURY:
            self._treasury += amount
        if credit == TREASURY:
            self._treasury -= amount
        self._seq += 1
        self._head = h
        return e

    def seed_opening(self, amount: float, ts: float) -> None:
        if self._seeded:
            return
        self._seeded = True
        if amount > 0:
            # Opening capital: treasury (asset) up, external funding source down.
            self._post(debit=TREASURY, credit=FUNDING_EXTERNAL,
                       amount=amount, category="funding", ts=ts)

    # -- journaling authoritative wallet movements -------------------------
    @staticmethod
    def _classify(mv: dict[str, Any]) -> tuple[str, str, float, str, str | None]:
        """Map one normalized wallet movement to (debit, credit, amount,
        category, counterparty)."""
        kind, m = movement_leg(mv)
        amount = float(m["amount_usdc"])
        if kind == "debit":
            if m.get("kind") == "payment":
                return ("expense:payment", TREASURY, amount, "payment",
                        m.get("to"))
            memo = str(m.get("memo", "metered"))
            cat = memo.split(":", 1)[0] or "metered"
            return (f"expense:{cat}", TREASURY, amount, cat, m.get("to"))
        if kind == "credit":
            src = str(m.get("from", "unknown"))
            if src.startswith(DEPOSIT_PREFIX):
                return (TREASURY, FUNDING_EXTERNAL, amount, "deposit", src)
            return (TREASURY, f"revenue:{src}", amount, f"revenue:{src}", src)
        if kind == "reproduction_transfer":
            return (EQUITY_REPRODUCTION, TREASURY, amount, "reproduction", None)
        if kind == "harvest":
            if m.get("kind") == "platform_fee":
                return ("expense:platform_fee", TREASURY, amount,
                        "platform_fee", m.get("destination"))
            return (EQUITY_OWNER_DRAW, TREASURY, amount, "owner_draw",
                    m.get("destination"))
        # Every other row a wallet emits (sweep_out, fiat_payout_transfer, …)
        # is a platform-driven outflow. Booking an unknown kind as an outflow
        # is the safe failure: if it was really an inflow, the sign is wrong,
        # reconciliation drifts, and the Darwin halts instead of mis-counting.
        return (f"equity:{kind}", TREASURY, amount, kind, m.get("destination"))

    def journal_new(self, movements: list[dict[str, Any]], *, now: float) -> int:
        """Journal movements not yet seen. Returns how many were posted."""
        new = movements[self._cursor :]
        for mv in new:
            debit, credit, amount, cat, cp = self._classify(mv)
            ts = float(movement_leg(mv)[1].get("ts", now))
            self._post(debit=debit, credit=credit, amount=amount,
                       category=cat, ts=ts, counterparty=cp)
        self._cursor = len(movements)
        return len(new)

    # -- balances + integrity ---------------------------------------------
    def treasury_balance(self) -> float:
        # O(1) running total (maintained in ``_post``), so this is exact even
        # after old entries fall out of the bounded window.
        return round(self._treasury, 9)

    def verify(self) -> None:
        """Re-verify the retained entry window: each entry's stored hash must
        recompute, ``seq`` must be contiguous, and ``prev_hash`` must link to
        the previous retained entry. The window may begin at a non-zero seq
        (older entries pruned), so linkage is checked relative to the window's
        first entry rather than against GENESIS — the audit chain + wallet feed
        are the durable full-history record this is derived from."""
        entries = list(self._entries)
        for i, e in enumerate(entries):
            expect = _hash(e.seq, e.prev_hash, e.ts, e.debit, e.credit,
                           e.amount_usdc, e.category, e.counterparty,
                           e.action_id)
            if e.hash != expect:
                raise LedgerError(f"ledger entry seq {e.seq} hash mismatch (tampered)")
            if i == 0:
                continue
            prevs = entries[i - 1]
            if e.seq != prevs.seq + 1 or e.prev_hash != prevs.hash:
                raise LedgerError(f"ledger chain broken at seq {e.seq}")

    def reconcile(self, wallet_balance: float, tolerance: float) -> float:
        """Return the drift; raise :class:`LedgerError` if it exceeds
        tolerance (doc 05: a mismatch larger than tolerance halts)."""
        drift = abs(self.treasury_balance() - float(wallet_balance))
        if drift > tolerance:
            raise LedgerError(
                f"ledger ${self.treasury_balance():.6f} vs wallet "
                f"${wallet_balance:.6f} drift ${drift:.6f} > tolerance "
                f"${tolerance:.6f}"
            )
        return drift

    @property
    def seeded(self) -> bool:
        return self._seeded

    @property
    def entries(self) -> list[LedgerEntry]:
        return list(self._entries)
