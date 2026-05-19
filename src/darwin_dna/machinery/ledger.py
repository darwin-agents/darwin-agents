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
from typing import Any

from pydantic import BaseModel, Field

from .._canonical import canonical_bytes

GENESIS_HASH = "0" * 64
TREASURY = "treasury"
FUNDING_EXTERNAL = "funding:external"
EQUITY_REPRODUCTION = "equity:reproduction"


class LedgerError(Exception):
    """The ledger is internally inconsistent or has drifted from the
    authoritative wallet beyond tolerance. The Darwin must halt (doc 05)."""


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

    def __init__(self, darwin_id: str):
        self._darwin_id = darwin_id
        self._entries: list[LedgerEntry] = []
        self._seq = 0
        self._head = GENESIS_HASH
        self._seeded = False
        self._cursor = 0  # how many wallet movements have been journaled

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
        if "debit" in mv:
            d = mv["debit"]
            amount = float(d["amount_usdc"])
            if d.get("kind") == "payment":
                return ("expense:payment", TREASURY, amount, "payment",
                        d.get("to"))
            memo = str(d.get("memo", "metered"))
            cat = memo.split(":", 1)[0] or "metered"
            return (f"expense:{cat}", TREASURY, amount, cat, d.get("to"))
        if "credit" in mv:
            c = mv["credit"]
            src = str(c.get("from", "unknown"))
            return (TREASURY, f"revenue:{src}", float(c["amount_usdc"]),
                    f"revenue:{src}", src)
        if "reproduction_transfer" in mv:
            r = mv["reproduction_transfer"]
            return (EQUITY_REPRODUCTION, TREASURY, float(r["amount_usdc"]),
                    "reproduction", None)
        raise LedgerError(f"unrecognised wallet movement: {sorted(mv)}")

    def journal_new(self, movements: list[dict[str, Any]], *, now: float) -> int:
        """Journal movements not yet seen. Returns how many were posted."""
        new = movements[self._cursor :]
        for mv in new:
            debit, credit, amount, cat, cp = self._classify(mv)
            ts = float(
                mv.get("debit", mv.get("credit", mv.get("reproduction_transfer", {})))
                .get("ts", now)
            )
            self._post(debit=debit, credit=credit, amount=amount,
                       category=cat, ts=ts, counterparty=cp)
        self._cursor = len(movements)
        return len(new)

    # -- balances + integrity ---------------------------------------------
    def treasury_balance(self) -> float:
        bal = 0.0
        for e in self._entries:
            if e.debit == TREASURY:
                bal += e.amount_usdc
            if e.credit == TREASURY:
                bal -= e.amount_usdc
        return round(bal, 9)

    def verify(self) -> None:
        prev = GENESIS_HASH
        for i, e in enumerate(self._entries):
            if e.seq != i or e.prev_hash != prev:
                raise LedgerError(f"ledger chain broken at seq {i}")
            expect = _hash(e.seq, e.prev_hash, e.ts, e.debit, e.credit,
                           e.amount_usdc, e.category, e.counterparty,
                           e.action_id)
            if e.hash != expect:
                raise LedgerError(f"ledger entry {i} hash mismatch (tampered)")
            prev = e.hash

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
