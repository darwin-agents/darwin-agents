"""SQLite-backed persistence for the platform's durable state.

Doc 08 calls out the audit log as "permanent and hash-chained — we do not
delete audit entries even when a Darwin dies." The in-memory implementations
in :mod:`darwin_platform.operator_audit`, :mod:`darwin_platform.billing`,
and :mod:`darwin_platform.auth` are convenient for tests but die with the
process, which contradicts that promise.

This module wraps each of those collection types with a SQLite-backed
variant that:

- **Reads through to memory** on init: existing rows are loaded so all
  query paths run against an in-process snapshot for fast lookups, just
  like the in-memory implementations.
- **Writes through to SQLite** on every mutation: the database is the
  durable source of truth. Process crashes between writes lose at most
  one in-flight call's work; restart loads the persisted state and
  carries on.
- **Preserves chain integrity**. The persistent audit log loads its
  rows in seq order; ``verify()`` walks the same chain it would in
  memory.

SQLite was chosen because it ships in the stdlib, gives real ACID
guarantees per-row, and runs against a single file (one Supervisor =
one DB file). Production deployers can swap the persistent classes for
Postgres-backed variants behind the same interfaces without touching
the Supervisor.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from darwin_dna.constitution import TicketClass

from .auth import AuthContext, TokenStore
from .billing import BillingLedger, BillingPolicy, BillingService
from .escalation import (
    EscalationRouter,
    NotificationSink,
    RoutedTicket,
    TicketStatus,
)
from .monitoring import (
    Alert,
    AlertSink,
    Monitor,
    Quotas,
    Severity,
)
from .operator_audit import (
    OperatorAuditEntry,
    OperatorAuditError,
    OperatorAuditLog,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_entries (
    seq         INTEGER PRIMARY KEY,
    ts          REAL    NOT NULL,
    actor       TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    darwin_id   TEXT,
    metadata    TEXT    NOT NULL,  -- canonical JSON
    prev_hash   TEXT    NOT NULL,
    entry_hash  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_actor_idx     ON audit_entries(actor);
CREATE INDEX IF NOT EXISTS audit_action_idx    ON audit_entries(action);
CREATE INDEX IF NOT EXISTS audit_darwin_idx    ON audit_entries(darwin_id);
CREATE INDEX IF NOT EXISTS audit_ts_idx        ON audit_entries(ts);

CREATE TABLE IF NOT EXISTS tokens (
    token          TEXT PRIMARY KEY,
    scopes         TEXT NOT NULL,   -- JSON array
    owned_darwins  TEXT NOT NULL,   -- JSON array
    subject        TEXT
);

CREATE TABLE IF NOT EXISTS billing_ledgers (
    darwin_id                       TEXT PRIMARY KEY,
    metered_gross_usdc              REAL NOT NULL DEFAULT 0,
    metered_provider_cost_usdc      REAL NOT NULL DEFAULT 0,
    metered_platform_revenue_usdc   REAL NOT NULL DEFAULT 0,
    metered_events                  INTEGER NOT NULL DEFAULT 0,
    harvests_gross_usdc             REAL NOT NULL DEFAULT 0,
    harvests_net_usdc               REAL NOT NULL DEFAULT 0,
    harvests_fee_usdc               REAL NOT NULL DEFAULT 0,
    harvest_events                  INTEGER NOT NULL DEFAULT 0,
    harvest_history                 TEXT NOT NULL DEFAULT '[]'  -- JSON list
);

CREATE TABLE IF NOT EXISTS lineage_edges (
    parent_id     TEXT NOT NULL,
    child_id      TEXT NOT NULL,
    spawned_at    REAL NOT NULL,
    budget_usdc   REAL NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
CREATE INDEX IF NOT EXISTS lineage_child_idx ON lineage_edges(child_id);

CREATE TABLE IF NOT EXISTS routed_tickets (
    ticket_id            TEXT PRIMARY KEY,
    darwin_id            TEXT NOT NULL,
    neuron               TEXT NOT NULL,
    ticket_class         TEXT NOT NULL,
    opened_ts            REAL NOT NULL,
    deadline_ts          REAL NOT NULL,
    detail               TEXT NOT NULL,    -- JSON
    status               TEXT NOT NULL,    -- open | responded | expired
    response_decision    TEXT,
    response_data        TEXT,             -- JSON
    responded_ts         REAL,
    notification_sent_ts REAL,
    tick                 INTEGER
);
CREATE INDEX IF NOT EXISTS routed_tickets_darwin_idx ON routed_tickets(darwin_id);

CREATE TABLE IF NOT EXISTS monitor_alerts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    darwin_id         TEXT NOT NULL,
    kind              TEXT NOT NULL,
    severity          TEXT NOT NULL,
    suggested_level   INTEGER NOT NULL,
    detail            TEXT NOT NULL,    -- JSON
    ts                REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS monitor_alerts_darwin_idx ON monitor_alerts(darwin_id);
CREATE INDEX IF NOT EXISTS monitor_alerts_ts_idx     ON monitor_alerts(ts);
"""


def _connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    # WAL gives concurrent readers + a single writer; the supervisor's
    # async drive loop is single-writer per process so this is appropriate.
    conn.execute("PRAGMA journal_mode=WAL")
    # Foreign keys not used here but enable for future expansion.
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


class PersistenceStore:
    """One handle that owns the SQLite connection and exposes the three
    persistent collections used by the supervisor + API layer.

    Construct once per process and pass the relevant sub-objects to
    :class:`Supervisor` and :func:`build_app`. Calling :meth:`close`
    closes the underlying SQLite connection; future writes will fail."""

    def __init__(self, path: str | Path, *, clock=time.time) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = _connect(self._path)
        with self._conn:
            self._conn.executescript(_SCHEMA)
        self._clock = clock
        self._audit = PersistentOperatorAuditLog(self._conn, clock=clock)
        self._tokens = PersistentTokenStore(self._conn)
        self._billing = PersistentBillingService(
            self._conn, BillingPolicy(), clock=clock
        )

    @property
    def path(self) -> Path:
        return self._path

    @property
    def audit_log(self) -> "PersistentOperatorAuditLog":
        return self._audit

    @property
    def token_store(self) -> "PersistentTokenStore":
        return self._tokens

    @property
    def billing_service(self) -> "PersistentBillingService":
        return self._billing

    @property
    def connection(self) -> sqlite3.Connection:
        """Exposed for the supervisor to construct persistent variants
        that need additional ctor args (sink, quotas, clock) — keeps the
        SQLite handle in one place."""
        return self._conn

    def lineage_registry(self) -> "PersistentLineageRegistry":
        return PersistentLineageRegistry(self._conn)

    def escalation_router(
        self,
        *,
        sink: NotificationSink | None = None,
        clock=time.time,
    ) -> "PersistentEscalationRouter":
        return PersistentEscalationRouter(self._conn, sink=sink, clock=clock)

    def monitor(
        self,
        *,
        quotas: Quotas | None = None,
        sink: AlertSink | None = None,
        clock=time.time,
    ) -> "PersistentMonitor":
        return PersistentMonitor(
            self._conn, quotas=quotas, sink=sink, clock=clock
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# -- audit log ------------------------------------------------------------
class PersistentOperatorAuditLog(OperatorAuditLog):
    """SQLite-backed variant. Loads existing rows on construction so the
    in-memory chain matches the database; appends to both on
    :meth:`record`. ``verify()`` runs against the in-memory mirror — both
    are kept in lockstep by the write-through path."""

    def __init__(self, conn: sqlite3.Connection, *, clock=time.time) -> None:
        super().__init__(clock=clock)
        self._conn = conn
        # Load existing entries in seq order so the parent class's chain
        # is correct and tip_hash points at the persisted tip.
        rows = list(conn.execute(
            "SELECT seq, ts, actor, action, darwin_id, metadata, "
            "prev_hash, entry_hash FROM audit_entries ORDER BY seq ASC"
        ))
        for r in rows:
            try:
                metadata = json.loads(r["metadata"])
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            entry = OperatorAuditEntry(
                seq=int(r["seq"]),
                ts=float(r["ts"]),
                actor=str(r["actor"]),
                action=str(r["action"]),
                darwin_id=r["darwin_id"],
                metadata=metadata,
                prev_hash=str(r["prev_hash"]),
                entry_hash=str(r["entry_hash"]),
            )
            self._entries.append(entry)

    def record(
        self,
        *,
        actor: str,
        action: str,
        darwin_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> OperatorAuditEntry:
        entry = super().record(
            actor=actor, action=action, darwin_id=darwin_id,
            metadata=metadata, ts=ts,
        )
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO audit_entries(seq, ts, actor, action, "
                    "darwin_id, metadata, prev_hash, entry_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.seq, entry.ts, entry.actor, entry.action,
                        entry.darwin_id,
                        json.dumps(entry.metadata, sort_keys=True),
                        entry.prev_hash, entry.entry_hash,
                    ),
                )
        except sqlite3.Error as exc:
            # The in-memory chain has the entry; the DB does not. That
            # would silently break the invariant "verify() == DB walk".
            # Refuse to leave the system in that state: pop the entry
            # from memory and re-raise so the operator sees the failure.
            self._entries.pop()
            raise OperatorAuditError(
                f"audit DB write failed: {exc}"
            ) from exc
        return entry


# -- token store ----------------------------------------------------------
class PersistentTokenStore:
    """SQLite-backed token store. Implements :class:`TokenStore` and
    supports the same ``issue`` / ``revoke`` API as
    :class:`InMemoryTokenStore`. Loads existing tokens on construction
    into an in-memory mirror; writes go to both."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._cache: dict[str, AuthContext] = {}
        for r in conn.execute(
            "SELECT token, scopes, owned_darwins, subject FROM tokens"
        ):
            try:
                scopes = set(json.loads(r["scopes"]))
                owned = set(json.loads(r["owned_darwins"]))
            except (TypeError, json.JSONDecodeError):
                continue
            self._cache[r["token"]] = AuthContext(
                token=r["token"],
                scopes=scopes,
                owned_darwins=owned,
                subject=r["subject"],
            )

    def lookup(self, token: str) -> AuthContext | None:
        return self._cache.get(token)

    def issue(
        self,
        token: str,
        *,
        scopes: Iterable[str],
        owned_darwins: Iterable[str] | None = None,
        subject: str | None = None,
    ) -> AuthContext:
        ctx = AuthContext(
            token=token,
            scopes=set(scopes),
            owned_darwins=set(owned_darwins or ()),
            subject=subject,
        )
        self._cache[token] = ctx
        self._persist(ctx)
        return ctx

    def grant(self, token: str, darwin_id: str) -> None:
        ctx = self._cache.get(token)
        if ctx is None:
            return
        ctx.owned_darwins.add(darwin_id)
        self._persist(ctx)

    def revoke(self, token: str) -> None:
        self._cache.pop(token, None)
        with self._conn:
            self._conn.execute("DELETE FROM tokens WHERE token = ?", (token,))

    def _persist(self, ctx: AuthContext) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO tokens(token, scopes, owned_darwins, subject) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(token) DO UPDATE SET "
                "scopes=excluded.scopes, "
                "owned_darwins=excluded.owned_darwins, "
                "subject=excluded.subject",
                (
                    ctx.token,
                    json.dumps(sorted(ctx.scopes)),
                    json.dumps(sorted(ctx.owned_darwins)),
                    ctx.subject,
                ),
            )


# -- billing service ------------------------------------------------------
class PersistentBillingService(BillingService):
    """SQLite-backed billing service. Loads ledgers on init; flushes the
    affected ledger to SQLite after every record_metered / record_harvest."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        policy: BillingPolicy | None = None,
        *,
        clock=time.time,
    ) -> None:
        super().__init__(policy, clock=clock)
        self._conn = conn
        for r in conn.execute(
            "SELECT darwin_id, metered_gross_usdc, metered_provider_cost_usdc, "
            "metered_platform_revenue_usdc, metered_events, "
            "harvests_gross_usdc, harvests_net_usdc, harvests_fee_usdc, "
            "harvest_events, harvest_history FROM billing_ledgers"
        ):
            try:
                history = json.loads(r["harvest_history"])
                if not isinstance(history, list):
                    history = []
            except (TypeError, json.JSONDecodeError):
                history = []
            self._ledgers[r["darwin_id"]] = BillingLedger(
                darwin_id=r["darwin_id"],
                metered_gross_usdc=float(r["metered_gross_usdc"]),
                metered_provider_cost_usdc=float(r["metered_provider_cost_usdc"]),
                metered_platform_revenue_usdc=float(
                    r["metered_platform_revenue_usdc"]
                ),
                metered_events=int(r["metered_events"]),
                harvests_gross_usdc=float(r["harvests_gross_usdc"]),
                harvests_net_usdc=float(r["harvests_net_usdc"]),
                harvests_fee_usdc=float(r["harvests_fee_usdc"]),
                harvest_events=int(r["harvest_events"]),
                harvest_history=history,
            )

    def record_metered(
        self, darwin_id: str, amount_usdc: float, *, memo: str | None = None
    ) -> tuple[float, float]:
        out = super().record_metered(darwin_id, amount_usdc, memo=memo)
        self._flush(darwin_id)
        return out

    def record_harvest(
        self,
        darwin_id: str,
        *,
        gross_usdc: float,
        fee_usdc: float,
        destination: str,
    ) -> dict[str, Any]:
        row = super().record_harvest(
            darwin_id, gross_usdc=gross_usdc, fee_usdc=fee_usdc,
            destination=destination,
        )
        self._flush(darwin_id)
        return row

    def _flush(self, darwin_id: str) -> None:
        led = self._ledgers.get(darwin_id)
        if led is None:
            return
        with self._conn:
            self._conn.execute(
                "INSERT INTO billing_ledgers(darwin_id, metered_gross_usdc, "
                "metered_provider_cost_usdc, metered_platform_revenue_usdc, "
                "metered_events, harvests_gross_usdc, harvests_net_usdc, "
                "harvests_fee_usdc, harvest_events, harvest_history) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(darwin_id) DO UPDATE SET "
                "metered_gross_usdc=excluded.metered_gross_usdc, "
                "metered_provider_cost_usdc=excluded.metered_provider_cost_usdc, "
                "metered_platform_revenue_usdc=excluded.metered_platform_revenue_usdc, "
                "metered_events=excluded.metered_events, "
                "harvests_gross_usdc=excluded.harvests_gross_usdc, "
                "harvests_net_usdc=excluded.harvests_net_usdc, "
                "harvests_fee_usdc=excluded.harvests_fee_usdc, "
                "harvest_events=excluded.harvest_events, "
                "harvest_history=excluded.harvest_history",
                (
                    darwin_id,
                    led.metered_gross_usdc,
                    led.metered_provider_cost_usdc,
                    led.metered_platform_revenue_usdc,
                    led.metered_events,
                    led.harvests_gross_usdc,
                    led.harvests_net_usdc,
                    led.harvests_fee_usdc,
                    led.harvest_events,
                    json.dumps(led.harvest_history),
                ),
            )


# -- lineage registry -----------------------------------------------------
# Imported here (bottom of module) to avoid a top-level circular import:
# supervisor.py defines LineageRegistry but also imports from this module.
from .supervisor import LineageEdge, LineageRegistry  # noqa: E402


class PersistentLineageRegistry(LineageRegistry):
    """SQLite-backed lineage registry. The edges table survives restarts;
    on init we replay it into the in-memory mirror so query paths stay
    fast. ``add`` is write-through."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self._conn = conn
        for r in conn.execute(
            "SELECT parent_id, child_id, spawned_at, budget_usdc "
            "FROM lineage_edges ORDER BY spawned_at ASC"
        ):
            edge = LineageEdge(
                parent_id=r["parent_id"],
                child_id=r["child_id"],
                spawned_at=float(r["spawned_at"]),
                budget_usdc=float(r["budget_usdc"]),
            )
            # Use the parent's add() so the in-memory indexes also populate.
            super().add(edge)

    def add(self, edge: LineageEdge) -> None:
        # Dedup is decided by the in-memory layer (super().add).
        before = len(self._edges)
        super().add(edge)
        if len(self._edges) == before:
            return  # was a duplicate; do not re-persist
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO lineage_edges"
                "(parent_id, child_id, spawned_at, budget_usdc) "
                "VALUES (?, ?, ?, ?)",
                (
                    edge.parent_id, edge.child_id,
                    float(edge.spawned_at), float(edge.budget_usdc),
                ),
            )


# -- escalation router ----------------------------------------------------
class PersistentEscalationRouter(EscalationRouter):
    """SQLite-backed escalation router. Loads existing tickets on init so a
    restart preserves both open and resolved ticket views; ``register`` and
    ``mark_responded`` write-through. Notification side-effects are NOT
    replayed on reload — the sink record is one-shot; we restore the
    ``notification_sent_ts`` from the row so the dashboard knows when the
    notification originally fired."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        sink: NotificationSink | None = None,
        clock=time.time,
    ) -> None:
        super().__init__(sink=sink, clock=clock)
        self._conn = conn
        for r in conn.execute(
            "SELECT ticket_id, darwin_id, neuron, ticket_class, opened_ts, "
            "deadline_ts, detail, status, response_decision, response_data, "
            "responded_ts, notification_sent_ts, tick FROM routed_tickets"
        ):
            try:
                detail = json.loads(r["detail"]) if r["detail"] else {}
            except (TypeError, json.JSONDecodeError):
                detail = {}
            try:
                response_data = (
                    json.loads(r["response_data"])
                    if r["response_data"] else None
                )
            except (TypeError, json.JSONDecodeError):
                response_data = None
            try:
                klass = TicketClass(r["ticket_class"])
            except ValueError:
                # Unknown class on reload: bucket as INFO (the lenient
                # default the live path also uses).
                klass = TicketClass.INFO
            try:
                status = TicketStatus(r["status"])
            except ValueError:
                status = TicketStatus.OPEN
            tk = RoutedTicket(
                ticket_id=r["ticket_id"],
                darwin_id=r["darwin_id"],
                neuron=r["neuron"],
                ticket_class=klass,
                opened_ts=float(r["opened_ts"]),
                deadline_ts=float(r["deadline_ts"]),
                detail=detail,
                status=status,
                response_decision=r["response_decision"],
                response_data=response_data,
                responded_ts=r["responded_ts"],
                notification_sent_ts=r["notification_sent_ts"],
                tick=r["tick"],
            )
            self._tickets[tk.ticket_id] = tk
            self._by_darwin.setdefault(tk.darwin_id, []).append(tk.ticket_id)

    def register(self, payload: dict[str, Any]) -> RoutedTicket:
        tid = payload.get("ticket")
        existed = tid in self._tickets if tid else False
        tk = super().register(payload)
        # ``register`` is idempotent on ticket id; only the FIRST sighting
        # creates a new row.
        if existed:
            return tk
        self._persist_ticket(tk)
        return tk

    def mark_responded(
        self,
        ticket_id: str,
        decision: str,
        data: dict[str, Any] | None = None,
    ) -> RoutedTicket | None:
        tk = super().mark_responded(ticket_id, decision, data)
        if tk is not None:
            self._persist_ticket(tk)
        return tk

    def _persist_ticket(self, tk: RoutedTicket) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO routed_tickets("
                "ticket_id, darwin_id, neuron, ticket_class, opened_ts, "
                "deadline_ts, detail, status, response_decision, "
                "response_data, responded_ts, notification_sent_ts, tick) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ticket_id) DO UPDATE SET "
                "status=excluded.status, "
                "response_decision=excluded.response_decision, "
                "response_data=excluded.response_data, "
                "responded_ts=excluded.responded_ts, "
                "notification_sent_ts=excluded.notification_sent_ts",
                (
                    tk.ticket_id, tk.darwin_id, tk.neuron,
                    tk.ticket_class.value,
                    tk.opened_ts, tk.deadline_ts,
                    json.dumps(tk.detail),
                    tk.status.value,
                    tk.response_decision,
                    json.dumps(tk.response_data) if tk.response_data else None,
                    tk.responded_ts,
                    tk.notification_sent_ts,
                    tk.tick,
                ),
            )


# -- monitor --------------------------------------------------------------
class PersistentMonitor(Monitor):
    """SQLite-backed monitor. Alerts are write-through on emit so dashboards
    and compliance reviewers see the same series across restarts. Tick
    freshness bookkeeping is in-process only — heartbeat health is meaningful
    only relative to the running process's wall-clock."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        quotas: Quotas | None = None,
        sink: AlertSink | None = None,
        clock=time.time,
    ) -> None:
        super().__init__(quotas=quotas, sink=sink, clock=clock)
        self._conn = conn
        for r in conn.execute(
            "SELECT darwin_id, kind, severity, suggested_level, detail, ts "
            "FROM monitor_alerts ORDER BY ts ASC"
        ):
            try:
                detail = json.loads(r["detail"]) if r["detail"] else {}
            except (TypeError, json.JSONDecodeError):
                detail = {}
            try:
                sev = Severity(r["severity"])
            except ValueError:
                sev = Severity.INFO
            alert = Alert(
                darwin_id=r["darwin_id"],
                kind=r["kind"],
                severity=sev,
                suggested_level=int(r["suggested_level"]),
                detail=detail,
                ts=float(r["ts"]),
            )
            self._alerts.setdefault(alert.darwin_id, []).append(alert)

    def check(self, signals) -> list[Alert]:
        fired = super().check(signals)
        if not fired:
            return fired
        with self._conn:
            self._conn.executemany(
                "INSERT INTO monitor_alerts"
                "(darwin_id, kind, severity, suggested_level, detail, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        a.darwin_id, a.kind, a.severity.value,
                        a.suggested_level, json.dumps(a.detail), a.ts,
                    )
                    for a in fired
                ],
            )
        return fired
