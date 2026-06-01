"""Persistence for the platform's durable state — SQLite or Postgres.

Doc 08 calls out the audit log as "permanent and hash-chained — we do not
delete audit entries even when a Darwin dies." The in-memory implementations
in :mod:`darwin_platform.operator_audit`, :mod:`darwin_platform.billing`,
and :mod:`darwin_platform.auth` are convenient for tests but die with the
process, which contradicts that promise.

This module wraps each of those collection types with a persistent variant
that:

- **Reads through to memory** on init: existing rows are loaded so all
  query paths run against an in-process snapshot for fast lookups, just
  like the in-memory implementations.
- **Writes through to the backend** on every mutation: the database is
  the durable source of truth. Process crashes between writes lose at
  most one in-flight call's work; restart loads the persisted state and
  carries on.
- **Preserves chain integrity**. The persistent audit log loads its
  rows in seq order; ``verify()`` walks the same chain it would in
  memory.

**Two backends:**

- **SQLite** (default, stdlib only). Single file, WAL journal. Right
  for one-process deployments — restart-survivable, no external
  service. Pass a file path to :class:`PersistenceStore`.
- **Postgres** (opt-in, requires the ``[postgres]`` extra → ``psycopg``).
  Right for multi-replica deployments (when the leader-election layer
  lands), production-grade backups, and the doc-08 §"Postgres"
  reference. Pass a ``postgresql://`` or ``postgres://`` DSN.

Both backends sit behind the same :class:`_Backend` adapter so the
``PersistentXxx`` classes don't care which is wired in. The SQL is the
same minus a couple of type-flavor differences (REAL ↔ DOUBLE PRECISION,
AUTOINCREMENT ↔ BIGSERIAL) handled by template substitution. Placeholders
are ``?`` everywhere; the Postgres backend translates to ``%s`` at execute
time.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from darwin_dna.constitution import TicketClass

from ..escalations.escalation import (
    EscalationRouter,
    NotificationSink,
    RoutedTicket,
    TicketStatus,
)
from ..observability.monitoring import (
    Alert,
    AlertSink,
    Monitor,
    Quotas,
    Severity,
)
from ..observability.operator_audit import (
    OperatorAuditEntry,
    OperatorAuditError,
    OperatorAuditLog,
)
from ..payments.billing import BillingLedger, BillingPolicy, BillingService
from ..security.auth import AuthContext


# -- schema (template; backends substitute the type flavors) ---------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_entries (
    seq         INTEGER PRIMARY KEY,
    ts          {real}  NOT NULL,
    actor       TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    darwin_id   TEXT,
    metadata    TEXT    NOT NULL,
    prev_hash   TEXT    NOT NULL,
    entry_hash  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_actor_idx     ON audit_entries(actor);
CREATE INDEX IF NOT EXISTS audit_action_idx    ON audit_entries(action);
CREATE INDEX IF NOT EXISTS audit_darwin_idx    ON audit_entries(darwin_id);
CREATE INDEX IF NOT EXISTS audit_ts_idx        ON audit_entries(ts);

CREATE TABLE IF NOT EXISTS tokens (
    token          TEXT PRIMARY KEY,
    scopes         TEXT NOT NULL,
    owned_darwins  TEXT NOT NULL,
    subject        TEXT
);

CREATE TABLE IF NOT EXISTS billing_ledgers (
    darwin_id                       TEXT PRIMARY KEY,
    metered_gross_usdc              {real}    NOT NULL DEFAULT 0,
    metered_provider_cost_usdc      {real}    NOT NULL DEFAULT 0,
    metered_platform_revenue_usdc   {real}    NOT NULL DEFAULT 0,
    metered_events                  INTEGER   NOT NULL DEFAULT 0,
    harvests_gross_usdc             {real}    NOT NULL DEFAULT 0,
    harvests_net_usdc               {real}    NOT NULL DEFAULT 0,
    harvests_fee_usdc               {real}    NOT NULL DEFAULT 0,
    harvest_events                  INTEGER   NOT NULL DEFAULT 0,
    harvest_history                 TEXT      NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS lineage_edges (
    parent_id     TEXT  NOT NULL,
    child_id      TEXT  NOT NULL,
    spawned_at    {real} NOT NULL,
    budget_usdc   {real} NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
CREATE INDEX IF NOT EXISTS lineage_child_idx ON lineage_edges(child_id);

CREATE TABLE IF NOT EXISTS routed_tickets (
    ticket_id            TEXT PRIMARY KEY,
    darwin_id            TEXT NOT NULL,
    neuron               TEXT NOT NULL,
    ticket_class         TEXT NOT NULL,
    opened_ts            {real} NOT NULL,
    deadline_ts          {real} NOT NULL,
    detail               TEXT NOT NULL,
    status               TEXT NOT NULL,
    response_decision    TEXT,
    response_data        TEXT,
    responded_ts         {real},
    notification_sent_ts {real},
    tick                 INTEGER
);
CREATE INDEX IF NOT EXISTS routed_tickets_darwin_idx ON routed_tickets(darwin_id);

CREATE TABLE IF NOT EXISTS monitor_alerts (
    id                {auto_id_pk},
    darwin_id         TEXT  NOT NULL,
    kind              TEXT  NOT NULL,
    severity          TEXT  NOT NULL,
    suggested_level   INTEGER NOT NULL,
    detail            TEXT NOT NULL,
    ts                {real} NOT NULL
);
CREATE INDEX IF NOT EXISTS monitor_alerts_darwin_idx ON monitor_alerts(darwin_id);
CREATE INDEX IF NOT EXISTS monitor_alerts_ts_idx     ON monitor_alerts(ts);

CREATE TABLE IF NOT EXISTS darwin_lifecycle (
    darwin_id      TEXT PRIMARY KEY,
    opening_usdc   {real} NOT NULL,
    last_balance   {real},
    parent_id      TEXT,
    spawned_at     {real} NOT NULL,
    alive          INTEGER NOT NULL,
    killed         INTEGER NOT NULL,
    kill_reason    TEXT,
    escalation_auto TEXT,
    quarantined           INTEGER NOT NULL DEFAULT 0,
    quarantine_reason     TEXT,
    quarantine_operator   TEXT,
    quarantine_applied_ts {real}
);
CREATE INDEX IF NOT EXISTS darwin_lifecycle_alive_idx ON darwin_lifecycle(alive);

CREATE TABLE IF NOT EXISTS darwin_audit_records (
    darwin_id   TEXT     NOT NULL,
    seq         INTEGER  NOT NULL,
    record      TEXT     NOT NULL,
    ts          {real}   NOT NULL,
    PRIMARY KEY (darwin_id, seq)
);
CREATE INDEX IF NOT EXISTS darwin_audit_ts_idx
    ON darwin_audit_records(ts);

CREATE TABLE IF NOT EXISTS darwin_checkpoints (
    darwin_id   TEXT PRIMARY KEY,
    node        TEXT     NOT NULL,
    state       TEXT     NOT NULL,
    saved_at    {real}   NOT NULL
);

CREATE TABLE IF NOT EXISTS darwin_manifests (
    darwin_id   TEXT PRIMARY KEY,
    manifest    TEXT NOT NULL,
    layer_blobs TEXT NOT NULL,
    layer_names TEXT NOT NULL,
    created_at  {real} NOT NULL
);

CREATE TABLE IF NOT EXISTS platform_signing_key (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    private_key  TEXT     NOT NULL,
    created_at   {real}   NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    version       INTEGER  NOT NULL,
    applied_at    {real}   NOT NULL,
    code_version  TEXT     NOT NULL
);

CREATE TABLE IF NOT EXISTS platform_retired_keys (
    id              {auto_id_pk},
    public_key_pem  TEXT     NOT NULL,
    retired_at      {real}   NOT NULL,
    drain_until     {real}   NOT NULL
);
CREATE INDEX IF NOT EXISTS platform_retired_keys_drain_idx
    ON platform_retired_keys(drain_until);

CREATE TABLE IF NOT EXISTS darwin_preapprovals (
    darwin_id                TEXT     NOT NULL,
    rule_id                  TEXT     NOT NULL,
    max_amount_usdc          {real}   NOT NULL,
    counterparty_category    TEXT,
    counterparty_substring   TEXT,
    expires_at               {real},
    max_uses                 INTEGER,
    used_count               INTEGER  NOT NULL DEFAULT 0,
    created_at               {real}   NOT NULL,
    PRIMARY KEY (darwin_id, rule_id)
);

CREATE TABLE IF NOT EXISTS freelancer_registry (
    owner_subject     TEXT     NOT NULL,
    freelancer_id     TEXT     NOT NULL,
    fiat_destination  TEXT     NOT NULL,
    display_name      TEXT     NOT NULL DEFAULT '',
    kyc_status        TEXT     NOT NULL DEFAULT 'unverified',
    registered_at     {real}   NOT NULL,
    metadata_json     TEXT     NOT NULL DEFAULT '{{}}',
    PRIMARY KEY (owner_subject, freelancer_id)
);

-- Web Push subscriptions for the PWA escalation companion (doc 22). One
-- row per (owner_subject, endpoint). Keys (p256dh + auth) are needed
-- to ECE-encrypt the payload per RFC 8188. Indexed by owner so the
-- WebPushNotificationSink can fan out a single ticket to every device
-- the owner has signed in on.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    owner_subject     TEXT     NOT NULL,
    endpoint          TEXT     NOT NULL,
    p256dh            TEXT     NOT NULL,
    auth              TEXT     NOT NULL,
    user_agent        TEXT     NOT NULL DEFAULT '',
    registered_at     {real}   NOT NULL,
    PRIMARY KEY (owner_subject, endpoint)
);
CREATE INDEX IF NOT EXISTS push_subscriptions_owner_idx
    ON push_subscriptions(owner_subject);

-- Platform omnibus wallet (doc 22). The derived state (per-user credit
-- balances + lifetime totals) is snapshotted in a single row so it
-- survives a supervisor restart, while the append-only movement log keeps the
-- forensic history. The omnibus is platform-wide (not per-Darwin), so
-- one state row suffices.
CREATE TABLE IF NOT EXISTS omnibus_state (
    id                         INTEGER PRIMARY KEY CHECK (id = 1),
    user_credits               TEXT     NOT NULL DEFAULT '{{}}',
    lifetime_deposits          {real}   NOT NULL DEFAULT 0,
    lifetime_withdrawals       {real}   NOT NULL DEFAULT 0,
    lifetime_platform_revenue  {real}   NOT NULL DEFAULT 0,
    updated_ts                 {real}   NOT NULL
);

CREATE TABLE IF NOT EXISTS omnibus_entries (
    seq           {auto_id_pk},
    kind          TEXT     NOT NULL,
    amount_usdc   {real}   NOT NULL,
    counterparty  TEXT     NOT NULL,
    actor         TEXT     NOT NULL,
    ts            {real}   NOT NULL,
    memo          TEXT     NOT NULL DEFAULT '',
    tx_hash       TEXT     NOT NULL DEFAULT '',
    metadata      TEXT     NOT NULL DEFAULT '{{}}'
);
CREATE INDEX IF NOT EXISTS omnibus_entries_ts_idx ON omnibus_entries(ts);

-- Recurring profit-harvest rules (doc 05). Owner-registered automation
-- that the supervisor evaluates every drive cycle, persisted so a restart
-- doesn't silently drop the owner's "sweep above $X daily" intent.
CREATE TABLE IF NOT EXISTS harvest_rules (
    id                    TEXT PRIMARY KEY,
    darwin_id             TEXT    NOT NULL,
    destination           TEXT    NOT NULL,
    trigger_balance_usdc  {real}  NOT NULL,
    amount_usdc           {real},
    interval_seconds      {real}  NOT NULL,
    enabled               INTEGER NOT NULL DEFAULT 1,
    created_at            {real}  NOT NULL,
    last_run_at           {real},
    last_amount_usdc      {real}
);
CREATE INDEX IF NOT EXISTS harvest_rules_darwin_idx ON harvest_rules(darwin_id);

-- Per-Darwin resource throttles (doc 09 level 2). Operator-installed daily
-- caps on sandbox / browser / egress calls. Persisted so a restart can't
-- silently lift a cap an operator set in response to abuse. The per-day
-- usage counters are intentionally NOT persisted — they reset at midnight
-- anyway, so a restart resetting today's count is acceptable.
CREATE TABLE IF NOT EXISTS resource_throttles (
    darwin_id        TEXT    NOT NULL,
    kind             TEXT    NOT NULL,
    max_daily_calls  INTEGER NOT NULL,
    installed_at     {real}  NOT NULL,
    PRIMARY KEY (darwin_id, kind)
);

-- Open fiat-deposit quotes (doc 17). A quote is created off-session and
-- confirmed later by the Stripe webhook, and persisting it means a confirm
-- that lands after a restart still finds its quote (instead of the
-- deposit being silently lost), and a consumed/deleted quote makes a
-- webhook replay a safe no-op across restarts. Rows are deleted on
-- confirm — this table holds only OUTSTANDING quotes.
CREATE TABLE IF NOT EXISTS fiat_quotes (
    quote_id      TEXT PRIMARY KEY,
    provider      TEXT   NOT NULL,
    amount_usd    {real} NOT NULL,
    amount_usdc   {real} NOT NULL,
    fee_usd       {real} NOT NULL,
    rate          {real} NOT NULL,
    ttl_seconds   {real} NOT NULL,
    quoted_at     {real} NOT NULL
);

-- Processed payment-provider webhook events (doc 17). Keyed by the
-- provider's event id so a re-delivered webhook short-circuits before any
-- money effect — the Stripe-recommended idempotency pattern, layered on top
-- of quote-consumption. Durable so dedup survives a restart.
CREATE TABLE IF NOT EXISTS processed_webhook_events (
    event_id      TEXT PRIMARY KEY,
    provider      TEXT   NOT NULL,
    status        TEXT   NOT NULL,
    processed_at  {real} NOT NULL
);
"""


# The current schema version. Bump and add a migration each time the
# table layout changes. The migration framework runs every step from
# the persisted version up to this number on open.
CURRENT_SCHEMA_VERSION: int = 11
# String tag for the code that opened the DB. Tests can override; in
# real deployments this lets a forensic audit confirm which release
# touched a row.
CODE_VERSION_TAG: str = "darwin-platform/0.1"


class SchemaVersionError(Exception):
    """Raised when the persisted schema is newer than this code can
    handle — refusing to open is safer than silently mis-interpreting
    a future column shape."""


# -- backend abstraction --------------------------------------------------
class _Backend:
    """sqlite3.Connection-shaped surface over either SQLite or Postgres.

    The PersistentXxx classes call ``execute`` / ``executemany`` /
    ``executescript`` and use ``with backend: ...`` for transactions, the
    same way the original SQLite-only code did. Backends translate where
    needed (``?`` → ``%s`` for Postgres; flavor-specific DDL).
    """

    kind: str = ""
    real_type: str = "REAL"
    auto_id_pk: str = "INTEGER PRIMARY KEY AUTOINCREMENT"

    def schema(self) -> str:
        return _SCHEMA.format(real=self.real_type, auto_id_pk=self.auto_id_pk)

    # Subclasses implement:
    def execute(self, sql: str, params: tuple = ()) -> Any: ...
    def executemany(self, sql: str, params_seq: Iterable[tuple]) -> None: ...
    def executescript(self, script: str) -> None: ...
    def __enter__(self): ...
    def __exit__(self, exc_type, exc, tb): ...
    def close(self) -> None: ...


class _SqliteBackend(_Backend):
    """The classic. WAL journal + foreign-keys on; one file, no daemon."""

    kind = "sqlite"
    real_type = "REAL"
    auto_id_pk = "INTEGER PRIMARY KEY AUTOINCREMENT"

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so FastAPI's threadpool (TestClient,
        # uvicorn's default sync-route executor) can use the connection
        # safely; we serialize writes via the connection's implicit
        # lock + the ``with self._backend:`` transaction pattern.
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        return self._conn.execute(sql, params)

    def executemany(self, sql, params_seq):
        self._conn.executemany(sql, params_seq)

    def executescript(self, script):
        self._conn.executescript(script)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._conn.__exit__(exc_type, exc, tb)

    def close(self):
        self._conn.close()


# Postgres connection-retry policy (docs/engineering/28: reconnect — up to
# 5 attempts, exponential backoff 1s/2s/4s/8s/16s). Lets a supervisor pod
# that starts before its database is ready wait it out instead of crash-
# looping, and ride through a brief failover.
_PG_RECONNECT_ATTEMPTS = 5
_PG_RECONNECT_BASE_DELAY = 1.0


def _split_sql_statements(script: str) -> list[str]:
    """Split a multi-statement SQL script into individual statements,
    safely ignoring ``;`` that appears inside ``--`` line comments.

    Without this, a schema comment like ``-- foo; bar`` would cause the
    naive `script.split(";")` to mis-parse every statement after it. No
    SQL in this module embeds a literal ``;`` inside a string, so a full
    SQL parser is overkill — line-by-line ``--``-stripping is enough.

    Returns a list of trimmed, non-empty statements.
    """
    stripped: list[str] = []
    for line in script.splitlines():
        idx = line.find("--")
        if idx >= 0:
            line = line[:idx]
        stripped.append(line)
    cleaned = "\n".join(stripped)
    return [s.strip() for s in cleaned.split(";") if s.strip()]


def _retry_connect(
    connect_fn: Callable[[], Any],
    *,
    attempts: int,
    base_delay: float,
    sleep: Callable[[float], None],
    retry_on: tuple[type[BaseException], ...],
) -> Any:
    """Call ``connect_fn``, retrying failures of type ``retry_on`` with
    exponential backoff (doc 28). Returns the connection; raises the last
    error if every attempt fails."""
    last_exc: BaseException | None = None
    for attempt in range(max(1, attempts)):
        try:
            return connect_fn()
        except retry_on as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            sleep(base_delay * (2 ** attempt))
    assert last_exc is not None
    raise last_exc


class _PostgresBackend(_Backend):
    """psycopg-backed. Multi-replica future-proofs the supervisor; for
    now the same single-writer constraint applies (Postgres just lets us
    survive a pod replacement without losing the audit chain on a PVC
    that didn't get backed up). DSN forms: ``postgresql://...`` or
    ``postgres://...``."""

    kind = "postgres"
    real_type = "DOUBLE PRECISION"
    auto_id_pk = "BIGSERIAL PRIMARY KEY"

    def __init__(
        self,
        dsn: str,
        *,
        connect_attempts: int = _PG_RECONNECT_ATTEMPTS,
        connect_base_delay: float = _PG_RECONNECT_BASE_DELAY,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - install hint
            raise ImportError(
                "Postgres persistence requires the [postgres] extra: "
                "`pip install darwin-dna[postgres]`"
            ) from exc
        self._dsn = dsn
        self._connect_attempts = max(1, int(connect_attempts))
        self._connect_base_delay = max(0.0, float(connect_base_delay))
        self._sleep = sleep
        self._op_error: type[BaseException] = psycopg.OperationalError
        # autocommit=True so SELECTs don't leave a long-lived implicit
        # transaction open (psycopg's default off-mode does, and close()
        # would then roll back any pending writes that ran in nested
        # ``conn.transaction()`` savepoints). Single statements commit
        # immediately; multi-statement atomicity is opt-in via the
        # ``with self._backend:`` transaction wrapper.
        self._conn = self._connect()

    def _connect(self) -> Any:
        """(Re)open the connection, retrying on failure with exponential
        backoff (docs/engineering/28: up to 5 attempts, 1s/2s/4s/8s/16s)."""
        import psycopg
        from psycopg.rows import dict_row

        return _retry_connect(
            lambda: psycopg.connect(
                self._dsn, autocommit=True, row_factory=dict_row
            ),
            attempts=self._connect_attempts,
            base_delay=self._connect_base_delay,
            sleep=self._sleep,
            retry_on=(psycopg.OperationalError,),
        )

    def reconnect(self) -> None:
        """Drop and re-establish the connection after a connection-level
        error (doc 28). Retries with the same exponential backoff."""
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - the conn is already dead; ignore
            pass
        self._conn = self._connect()

    @staticmethod
    def _adapt(sql: str) -> str:
        # All our SQL uses ``?`` placeholders. Translate to psycopg's
        # ``%s`` at execute time. No SQL in this module contains ``?``
        # in string literals (Postgres doesn't allow that anyway), so a
        # straight replace is safe.
        return sql.replace("?", "%s")

    def execute(self, sql, params=()):
        try:
            cur = self._conn.cursor()
            cur.execute(self._adapt(sql), tuple(params))
            return cur
        except self._op_error:
            # Connection-level failure: reconnect (doc 28). A read-only
            # statement is idempotent — retry it once. A write is NOT
            # retried: replaying one that may already have committed risks
            # a double-apply, so we heal the connection and re-raise for
            # the caller to handle.
            self.reconnect()
            if sql.lstrip()[:6].lower() == "select":
                cur = self._conn.cursor()
                cur.execute(self._adapt(sql), tuple(params))
                return cur
            raise

    def executemany(self, sql, params_seq):
        with self._conn.cursor() as cur:
            cur.executemany(self._adapt(sql), [tuple(p) for p in params_seq])

    def executescript(self, script):
        # Postgres doesn't have an executescript shortcut. Split on
        # semicolon at statement boundaries — but FIRST strip ``--``
        # comments line-by-line via _split_sql_statements so a ``;`` inside
        # a comment doesn't break the split.
        with self._conn.cursor() as cur:
            for stmt in _split_sql_statements(script):
                cur.execute(self._adapt(stmt))

    def __enter__(self):
        # psycopg 3.3 changed ``with conn:`` to close the connection on
        # exit (was just commit/rollback in earlier versions). Use the
        # explicit ``conn.transaction()`` context manager instead — that
        # commits/rolls back without touching the connection lifecycle.
        # Nested transactions become savepoints; safe for re-entry.
        self._tx = self._conn.transaction()
        self._tx.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._tx.__exit__(exc_type, exc, tb)

    def close(self):
        self._conn.close()


def _open_backend(spec: str | Path) -> _Backend:
    """Pick a backend from the connection spec. ``postgres://`` or
    ``postgresql://`` → Postgres; anything else → SQLite (file path)."""
    s = str(spec)
    if s.startswith(("postgresql://", "postgres://")):
        return _PostgresBackend(s)
    return _SqliteBackend(s)


# -- the store + persistent collections -----------------------------------
class PersistenceStore:
    """One handle that owns the backend connection and exposes the
    persistent collections used by the supervisor + API layer.

    Construct once per process and pass the relevant sub-objects to
    :class:`Supervisor` and :func:`build_app`. Calling :meth:`close`
    closes the underlying connection; future writes will fail."""

    def __init__(self, spec: str | Path, *, clock=time.time) -> None:
        self._spec = spec
        self._backend = _open_backend(spec)
        with self._backend:
            self._backend.executescript(self._backend.schema())
        self._apply_migrations()
        self._clock = clock
        self._audit = PersistentOperatorAuditLog(self._backend, clock=clock)
        self._tokens = PersistentTokenStore(self._backend)
        self._billing = PersistentBillingService(
            self._backend, BillingPolicy(), clock=clock
        )

    @property
    def spec(self) -> str | Path:
        return self._spec

    # -- schema versioning (forward-compat refusal) -----------------------
    def _apply_migrations(self) -> None:
        """Read the persisted schema version; run forward migrations
        from there to ``CURRENT_SCHEMA_VERSION``. Refuses to open a DB
        whose persisted version is *newer* than this code knows about —
        an older binary against a newer schema would silently
        misinterpret columns."""
        row = self._backend.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()
        if row is None:
            persisted = 0
        else:
            persisted = int(row["version"] if isinstance(row, dict) else row[0])
        if persisted > CURRENT_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"persisted schema version {persisted} is newer than this "
                f"code supports ({CURRENT_SCHEMA_VERSION}); upgrade the "
                f"darwin-platform binary before opening this DB"
            )
        if persisted == CURRENT_SCHEMA_VERSION:
            return
        # Apply forward migrations sequentially.
        for v in range(persisted + 1, CURRENT_SCHEMA_VERSION + 1):
            migration = _MIGRATIONS.get(v)
            if migration is None:
                # No-op step — additive schema changes are absorbed by
                # the CREATE TABLE IF NOT EXISTS in the main schema.
                pass
            else:
                with self._backend:
                    migration(self._backend)
            with self._backend:
                self._backend.execute(
                    "INSERT INTO schema_version "
                    "(id, version, applied_at, code_version) "
                    "VALUES (1, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "version = excluded.version, "
                    "applied_at = excluded.applied_at, "
                    "code_version = excluded.code_version",
                    (v, float(time.time()), CODE_VERSION_TAG),
                )

    def schema_version(self) -> int:
        row = self._backend.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()
        if row is None:
            return 0
        return int(row["version"] if isinstance(row, dict) else row[0])

    @property
    def backend_kind(self) -> str:
        """Either ``"sqlite"`` or ``"postgres"``. Useful for /healthz."""
        return self._backend.kind

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
    def connection(self) -> _Backend:
        """Exposed for the supervisor to construct persistent variants
        that need additional ctor args (sink, quotas, clock) — keeps the
        backend handle in one place."""
        return self._backend

    def lineage_registry(self) -> "PersistentLineageRegistry":
        return PersistentLineageRegistry(self._backend)

    def preapproval_store(self) -> "PersistentPreApprovalStore":
        """Per-Darwin pre-approval rule store. The supervisor writes
        through on every set/revoke and rehydrates the wallet on spawn
        so a restart preserves the owner's typed escalation exceptions."""
        return PersistentPreApprovalStore(self._backend, clock=self._clock)

    def freelancer_registry(self) -> "PersistentFreelancerRegistry":
        """Owner-scoped registry of verified fiat-payout destinations.
        Wired into darwin_fiat_payout as the KYC gate."""
        return PersistentFreelancerRegistry(self._backend, clock=self._clock)

    def push_subscription_store(self) -> "PersistentPushSubscriptionStore":
        """Per-owner Web Push subscriptions registered by the PWA. The
        :class:`WebPushNotificationSink` is hydrated from this on supervisor
        boot and writes through on every register / revoke / 410-purge."""
        return PersistentPushSubscriptionStore(self._backend, clock=self._clock)

    def lifecycle_store(self) -> "PersistentLifecycleStore":
        return PersistentLifecycleStore(self._backend)

    def omnibus_store(self) -> "PersistentOmnibusStore":
        """Persistence for the platform omnibus wallet. The OmnibusWallet
        attaches this at boot to rehydrate user credit balances + lifetime
        totals and to write through every subsequent movement, so credit
        no longer evaporates on restart."""
        return PersistentOmnibusStore(self._backend)

    def harvest_store(self) -> "PersistentHarvestStore":
        """Persistence for recurring harvest rules. The HarvestScheduler
        attaches this so owner-registered sweeps survive a restart."""
        return PersistentHarvestStore(self._backend)

    def throttle_store(self) -> "PersistentThrottleStore":
        """Persistence for per-Darwin resource throttles. The ThrottleService
        attaches this so operator-installed caps survive a restart."""
        return PersistentThrottleStore(self._backend)

    def fiat_quote_store(self) -> "PersistentFiatQuoteStore":
        """Persistence for outstanding fiat deposit quotes. A fiat ramp
        attaches this so a deposit confirmed by webhook after a restart
        still resolves its quote (and a replay stays idempotent)."""
        return PersistentFiatQuoteStore(self._backend)

    def webhook_event_store(self) -> "PersistentWebhookEventStore":
        """Durable dedup ledger for payment-provider webhook events. The
        supervisor checks it before processing a webhook so a re-delivery
        short-circuits even across restarts."""
        return PersistentWebhookEventStore(self._backend)

    def audit_sink_for(self, darwin_id: str) -> "PersistentDarwinAuditSink":
        """Per-Darwin audit sink. The genome's AuditLogger rebuilds its
        chain head from ``sink.read_all()`` at boot — using this sink
        means a resumed Darwin continues the same chain it had before
        the restart, not a fresh one."""
        return PersistentDarwinAuditSink(self._backend, darwin_id=darwin_id)

    def checkpoint_store(self) -> "PersistentCheckpointStore":
        """Platform-wide checkpoint store. The genome's evolution loop
        calls ``platform.checkpoint.save / load`` per heartbeat — this
        variant survives across process restarts so a resumed Darwin
        picks up at the last saved node."""
        return PersistentCheckpointStore(self._backend)

    def manifest_store(self) -> "PersistentManifestStore":
        return PersistentManifestStore(self._backend)

    def load_signing_key(self) -> bytes | None:
        """Return the persisted 32-byte Ed25519 private key, or None when
        no key has been persisted yet. The supervisor uses this on
        startup so manifests signed before a process restart still
        verify after the restart."""
        import base64
        row = self._backend.execute(
            "SELECT private_key FROM platform_signing_key WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        b64 = row["private_key"] if isinstance(row, dict) else row[0]
        return base64.b64decode(b64)

    def save_signing_key(self, raw_private_key: bytes) -> None:
        import base64
        with self._backend:
            self._backend.execute(
                "INSERT INTO platform_signing_key (id, private_key, created_at) "
                "VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO NOTHING",
                (
                    base64.b64encode(raw_private_key).decode("ascii"),
                    float(time.time()),
                ),
            )

    def escalation_router(
        self,
        *,
        sink: NotificationSink | None = None,
        clock=time.time,
    ) -> "PersistentEscalationRouter":
        return PersistentEscalationRouter(
            self._backend, sink=sink, clock=clock
        )

    def monitor(
        self,
        *,
        quotas: Quotas | None = None,
        sink: AlertSink | None = None,
        clock=time.time,
    ) -> "PersistentMonitor":
        return PersistentMonitor(
            self._backend, quotas=quotas, sink=sink, clock=clock
        )

    def close(self) -> None:
        try:
            self._backend.close()
        except Exception:
            pass


# -- audit log ------------------------------------------------------------
class PersistentOperatorAuditLog(OperatorAuditLog):
    """Backend-backed variant. Loads existing rows on construction so the
    in-memory chain matches the database; appends to both on
    :meth:`record`. ``verify()`` runs against the in-memory mirror — both
    are kept in lockstep by the write-through path."""

    def __init__(self, backend: _Backend, *, clock=time.time) -> None:
        super().__init__(clock=clock)
        self._backend = backend
        rows = list(backend.execute(
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
            with self._backend:
                self._backend.execute(
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
        except Exception as exc:
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
    """Backend-backed token store. Implements :class:`TokenStore` and
    supports the same ``issue`` / ``revoke`` API as
    :class:`InMemoryTokenStore`. Loads existing tokens on construction
    into an in-memory mirror; writes go to both."""

    def __init__(self, backend: _Backend) -> None:
        self._backend = backend
        self._cache: dict[str, AuthContext] = {}
        for r in backend.execute(
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
        with self._backend:
            self._backend.execute(
                "DELETE FROM tokens WHERE token = ?", (token,)
            )

    def _persist(self, ctx: AuthContext) -> None:
        with self._backend:
            self._backend.execute(
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
    """Backend-backed billing service. Loads ledgers on init; flushes the
    affected ledger to the DB after every record_metered / record_harvest."""

    def __init__(
        self,
        backend: _Backend,
        policy: BillingPolicy | None = None,
        *,
        clock=time.time,
    ) -> None:
        super().__init__(policy, clock=clock)
        self._backend = backend
        for r in backend.execute(
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
        with self._backend:
            self._backend.execute(
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
    """Backend-backed lineage registry. The edges table survives restarts;
    on init we replay it into the in-memory mirror so query paths stay
    fast. ``add`` is write-through."""

    def __init__(self, backend: _Backend) -> None:
        super().__init__()
        self._backend = backend
        for r in backend.execute(
            "SELECT parent_id, child_id, spawned_at, budget_usdc "
            "FROM lineage_edges ORDER BY spawned_at ASC"
        ):
            edge = LineageEdge(
                parent_id=r["parent_id"],
                child_id=r["child_id"],
                spawned_at=float(r["spawned_at"]),
                budget_usdc=float(r["budget_usdc"]),
            )
            super().add(edge)

    def add(self, edge: LineageEdge) -> None:
        before = len(self._edges)
        super().add(edge)
        if len(self._edges) == before:
            return  # was a duplicate; do not re-persist
        with self._backend:
            self._backend.execute(
                "INSERT INTO lineage_edges"
                "(parent_id, child_id, spawned_at, budget_usdc) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (parent_id, child_id) DO NOTHING",
                (
                    edge.parent_id, edge.child_id,
                    float(edge.spawned_at), float(edge.budget_usdc),
                ),
            )


# -- pre-approvals (doc 17 §"Darwin paying a human") ----------------------
class PersistentPreApprovalStore:
    """SQL-backed store of owner pre-approval rules, keyed by
    ``(darwin_id, rule_id)``. The supervisor writes through on every
    set / revoke and reloads the per-Darwin set on spawn so a restart
    preserves the owner's "auto-approve Upwork under $50" intent.

    The Pydantic model is rebuilt on read — the engine never sees the
    SQL row shape."""

    def __init__(self, backend: _Backend, *, clock=time.time) -> None:
        self._backend = backend
        self._clock = clock

    def save(self, *, darwin_id: str, rule) -> None:
        """Insert-or-replace a rule. ``rule`` is a
        :class:`darwin_dna.constitution.PreApprovalRule`. Round-trips
        ``max_uses`` + ``used_count`` so a consumed-rule counter
        survives a supervisor restart."""
        with self._backend:
            self._backend.execute(
                "INSERT INTO darwin_preapprovals"
                "(darwin_id, rule_id, max_amount_usdc, "
                "counterparty_category, counterparty_substring, "
                "expires_at, max_uses, used_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (darwin_id, rule_id) DO UPDATE SET "
                "max_amount_usdc = excluded.max_amount_usdc, "
                "counterparty_category = excluded.counterparty_category, "
                "counterparty_substring = excluded.counterparty_substring, "
                "expires_at = excluded.expires_at, "
                "max_uses = excluded.max_uses, "
                "used_count = excluded.used_count",
                (
                    darwin_id, rule.rule_id, float(rule.max_amount_usdc),
                    rule.counterparty_category, rule.counterparty_substring,
                    rule.expires_at,
                    rule.max_uses,
                    int(rule.used_count),
                    float(self._clock()),
                ),
            )

    def delete(self, *, darwin_id: str, rule_id: str) -> bool:
        """Return True if a row was removed; False otherwise. Idempotent."""
        with self._backend:
            cursor = self._backend.execute(
                "DELETE FROM darwin_preapprovals "
                "WHERE darwin_id = ? AND rule_id = ?",
                (darwin_id, rule_id),
            )
        # The _Backend execute() returns a cursor-shaped object whose
        # rowcount tells us whether anything was actually deleted.
        rowcount = getattr(cursor, "rowcount", None)
        if rowcount is None:
            # Fallback: re-query to determine presence. Best-effort —
            # callers treat the result as a hint, not a guarantee.
            row = self._backend.execute(
                "SELECT 1 FROM darwin_preapprovals "
                "WHERE darwin_id = ? AND rule_id = ?",
                (darwin_id, rule_id),
            ).fetchone()
            return row is None
        return int(rowcount) > 0

    def delete_all(self, *, darwin_id: str) -> None:
        """Wipe every rule for a Darwin. Called on kill so a future
        re-spawn of the same id doesn't resurrect stale rules."""
        with self._backend:
            self._backend.execute(
                "DELETE FROM darwin_preapprovals WHERE darwin_id = ?",
                (darwin_id,),
            )

    def load(self, *, darwin_id: str) -> list:
        """Return every persisted rule for ``darwin_id`` as a list of
        :class:`PreApprovalRule`. Used by the supervisor on spawn to
        rehydrate the wallet's in-memory set."""
        from darwin_dna.constitution import PreApprovalRule

        rows = self._backend.execute(
            "SELECT rule_id, max_amount_usdc, counterparty_category, "
            "counterparty_substring, expires_at, max_uses, used_count "
            "FROM darwin_preapprovals WHERE darwin_id = ? "
            "ORDER BY rule_id ASC",
            (darwin_id,),
        ).fetchall()
        out = []
        for r in rows:
            out.append(PreApprovalRule(
                rule_id=r["rule_id"],
                max_amount_usdc=float(r["max_amount_usdc"]),
                counterparty_category=r["counterparty_category"],
                counterparty_substring=r["counterparty_substring"],
                expires_at=(
                    float(r["expires_at"])
                    if r["expires_at"] is not None else None
                ),
                max_uses=(
                    int(r["max_uses"])
                    if r["max_uses"] is not None else None
                ),
                used_count=int(r["used_count"] or 0),
            ))
        return out


# -- freelancer registry (doc 17 §"Darwin paying a human") ----------------
class PersistentFreelancerRegistry:
    """SQL-backed FreelancerRegistry. Stores verified fiat-payout
    destinations per (owner_subject, freelancer_id) so a supervisor
    restart preserves the owner's already-onboarded freelancers."""

    def __init__(self, backend: _Backend, *, clock=time.time) -> None:
        self._backend = backend
        self._clock = clock

    def register(self, dest):
        import json as _json
        with self._backend:
            self._backend.execute(
                "INSERT INTO freelancer_registry"
                "(owner_subject, freelancer_id, fiat_destination, "
                "display_name, kyc_status, registered_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (owner_subject, freelancer_id) DO UPDATE SET "
                "fiat_destination = excluded.fiat_destination, "
                "display_name = excluded.display_name, "
                "kyc_status = excluded.kyc_status, "
                "registered_at = excluded.registered_at, "
                "metadata_json = excluded.metadata_json",
                (
                    dest.owner_subject, dest.freelancer_id,
                    dest.fiat_destination, dest.display_name,
                    dest.kyc_status,
                    float(dest.registered_at or self._clock()),
                    _json.dumps(dict(dest.metadata or {})),
                ),
            )
        return dest

    def lookup(self, *, owner_subject: str, freelancer_id: str):
        from ..payments.freelancer_registry import FreelancerDestination
        import json as _json
        row = self._backend.execute(
            "SELECT owner_subject, freelancer_id, fiat_destination, "
            "display_name, kyc_status, registered_at, metadata_json "
            "FROM freelancer_registry "
            "WHERE owner_subject = ? AND freelancer_id = ?",
            (owner_subject, freelancer_id),
        ).fetchone()
        if row is None:
            return None
        return FreelancerDestination(
            owner_subject=row["owner_subject"],
            freelancer_id=row["freelancer_id"],
            fiat_destination=row["fiat_destination"],
            display_name=row["display_name"] or "",
            kyc_status=row["kyc_status"] or "unverified",
            registered_at=float(row["registered_at"]),
            metadata=_json.loads(row["metadata_json"] or "{}"),
        )

    def list(self, *, owner_subject: str):
        from ..payments.freelancer_registry import FreelancerDestination
        import json as _json
        rows = self._backend.execute(
            "SELECT owner_subject, freelancer_id, fiat_destination, "
            "display_name, kyc_status, registered_at, metadata_json "
            "FROM freelancer_registry "
            "WHERE owner_subject = ? "
            "ORDER BY freelancer_id ASC",
            (owner_subject,),
        ).fetchall()
        out = []
        for row in rows:
            out.append(FreelancerDestination(
                owner_subject=row["owner_subject"],
                freelancer_id=row["freelancer_id"],
                fiat_destination=row["fiat_destination"],
                display_name=row["display_name"] or "",
                kyc_status=row["kyc_status"] or "unverified",
                registered_at=float(row["registered_at"]),
                metadata=_json.loads(row["metadata_json"] or "{}"),
            ))
        return out

    def revoke(self, *, owner_subject: str, freelancer_id: str) -> bool:
        with self._backend:
            cursor = self._backend.execute(
                "DELETE FROM freelancer_registry "
                "WHERE owner_subject = ? AND freelancer_id = ?",
                (owner_subject, freelancer_id),
            )
        rowcount = getattr(cursor, "rowcount", None)
        if rowcount is None:
            row = self._backend.execute(
                "SELECT 1 FROM freelancer_registry "
                "WHERE owner_subject = ? AND freelancer_id = ?",
                (owner_subject, freelancer_id),
            ).fetchone()
            return row is None
        return int(rowcount) > 0


# -- push subscriptions (doc 22 §"Mobile (escalation push)") --------------
class PersistentPushSubscriptionStore:
    """SQL-backed store for W3C Push API subscriptions registered by the
    PWA. The :class:`WebPushNotificationSink` is hydrated from this on
    supervisor boot via :meth:`load_into_sink` and the route handlers
    write through every register / revoke. 410/404 purges from the sink
    are persisted via the ``subscription_purge`` callback so a stale
    subscription doesn't resurrect on a restart.

    The schema lives in :data:`_SCHEMA` (table ``push_subscriptions``,
    primary key ``(owner_subject, endpoint)``)."""

    def __init__(self, backend: _Backend, *, clock=time.time) -> None:
        self._backend = backend
        self._clock = clock

    def register(
        self,
        *,
        owner_subject: str,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: str = "",
    ) -> None:
        with self._backend:
            self._backend.execute(
                "INSERT INTO push_subscriptions"
                "(owner_subject, endpoint, p256dh, auth, user_agent, "
                "registered_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (owner_subject, endpoint) DO UPDATE SET "
                "p256dh = excluded.p256dh, "
                "auth = excluded.auth, "
                "user_agent = excluded.user_agent, "
                "registered_at = excluded.registered_at",
                (
                    owner_subject, endpoint, p256dh, auth, user_agent,
                    float(self._clock()),
                ),
            )

    def revoke(self, *, owner_subject: str, endpoint: str) -> bool:
        with self._backend:
            cursor = self._backend.execute(
                "DELETE FROM push_subscriptions "
                "WHERE owner_subject = ? AND endpoint = ?",
                (owner_subject, endpoint),
            )
        rowcount = getattr(cursor, "rowcount", None)
        if rowcount is None:
            row = self._backend.execute(
                "SELECT 1 FROM push_subscriptions "
                "WHERE owner_subject = ? AND endpoint = ?",
                (owner_subject, endpoint),
            ).fetchone()
            return row is None
        return int(rowcount) > 0

    def list(self, *, owner_subject: str) -> list[dict[str, Any]]:
        rows = self._backend.execute(
            "SELECT endpoint, p256dh, auth, user_agent, registered_at "
            "FROM push_subscriptions WHERE owner_subject = ? "
            "ORDER BY registered_at DESC",
            (owner_subject,),
        ).fetchall()
        return [
            {
                "endpoint": r["endpoint"],
                "p256dh": r["p256dh"],
                "auth": r["auth"],
                "user_agent": r["user_agent"] or "",
                "registered_at": float(r["registered_at"]),
            }
            for r in rows
        ]

    def all(self) -> list[tuple[str, dict[str, Any]]]:
        """For supervisor boot — every subscription across all owners,
        each paired with its owner_subject. Order is unspecified."""
        rows = self._backend.execute(
            "SELECT owner_subject, endpoint, p256dh, auth, user_agent, "
            "registered_at FROM push_subscriptions"
        ).fetchall()
        out: list[tuple[str, dict[str, Any]]] = []
        for r in rows:
            out.append((
                r["owner_subject"],
                {
                    "endpoint": r["endpoint"],
                    "p256dh": r["p256dh"],
                    "auth": r["auth"],
                    "user_agent": r["user_agent"] or "",
                    "registered_at": float(r["registered_at"]),
                },
            ))
        return out

    def load_into_sink(self, sink: Any) -> None:
        """Hydrate a :class:`WebPushNotificationSink` from persisted rows.
        Called once on supervisor boot."""
        from ..escalations.notification_webpush import PushSubscription
        items = []
        for owner_subject, row in self.all():
            items.append((
                owner_subject,
                PushSubscription(
                    endpoint=row["endpoint"],
                    p256dh=row["p256dh"],
                    auth=row["auth"],
                    registered_at=row["registered_at"],
                    user_agent=row["user_agent"],
                ),
            ))
        sink.load_subscriptions(items)


# -- escalation router ----------------------------------------------------
class PersistentEscalationRouter(EscalationRouter):
    """Backend-backed escalation router. Loads existing tickets on init so
    a restart preserves both open and resolved ticket views; ``register``
    and ``mark_responded`` write-through. Notification side-effects are
    NOT replayed on reload — the sink record is one-shot; we restore the
    ``notification_sent_ts`` from the row so the dashboard knows when the
    notification originally fired."""

    def __init__(
        self,
        backend: _Backend,
        *,
        sink: NotificationSink | None = None,
        clock=time.time,
    ) -> None:
        super().__init__(sink=sink, clock=clock)
        self._backend = backend
        for r in backend.execute(
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
        with self._backend:
            self._backend.execute(
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
    """Backend-backed monitor. Alerts are write-through on emit so
    dashboards + compliance reviewers see the same series across
    restarts. Tick freshness bookkeeping stays in-process — heartbeat
    health is meaningful only relative to the running process's
    wall-clock."""

    def __init__(
        self,
        backend: _Backend,
        *,
        quotas: Quotas | None = None,
        sink: AlertSink | None = None,
        clock=time.time,
    ) -> None:
        super().__init__(quotas=quotas, sink=sink, clock=clock)
        self._backend = backend
        for r in backend.execute(
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
        with self._backend:
            self._backend.executemany(
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


# -- darwin lifecycle (doc 08 HA: runtime resume after failover) -----------
class LifecycleRecord:
    """Per-Darwin lifecycle snapshot used to resume after process restart
    or leader failover. Holds JUST what's needed to re-spawn a fresh
    runtime with the same opening budget; the audit chain stays
    authoritative for in-life events."""

    __slots__ = (
        "darwin_id", "opening_usdc", "last_balance", "parent_id",
        "spawned_at", "alive", "killed", "kill_reason", "escalation_auto",
        "quarantined", "quarantine_reason", "quarantine_operator",
        "quarantine_applied_ts",
    )

    def __init__(
        self,
        *,
        darwin_id: str,
        opening_usdc: float,
        last_balance: float | None,
        parent_id: str | None,
        spawned_at: float,
        alive: bool,
        killed: bool,
        kill_reason: str | None,
        escalation_auto: str | None,
        quarantined: bool = False,
        quarantine_reason: str | None = None,
        quarantine_operator: str | None = None,
        quarantine_applied_ts: float | None = None,
    ) -> None:
        self.darwin_id = darwin_id
        self.opening_usdc = opening_usdc
        self.last_balance = last_balance
        self.parent_id = parent_id
        self.spawned_at = spawned_at
        self.alive = alive
        self.killed = killed
        self.kill_reason = kill_reason
        self.escalation_auto = escalation_auto
        # Doc-09 level-4 quarantine, persisted (doc 23): a quarantined
        # Darwin must come back from a supervisor restart still quarantined.
        self.quarantined = quarantined
        self.quarantine_reason = quarantine_reason
        self.quarantine_operator = quarantine_operator
        self.quarantine_applied_ts = quarantine_applied_ts

    def to_json(self) -> dict:
        return {
            "darwin_id": self.darwin_id,
            "opening_usdc": self.opening_usdc,
            "last_balance": self.last_balance,
            "parent_id": self.parent_id,
            "spawned_at": self.spawned_at,
            "alive": self.alive,
            "killed": self.killed,
            "kill_reason": self.kill_reason,
            "escalation_auto": self.escalation_auto,
            "quarantined": self.quarantined,
            "quarantine_reason": self.quarantine_reason,
            "quarantine_operator": self.quarantine_operator,
            "quarantine_applied_ts": self.quarantine_applied_ts,
        }


class PersistentOmnibusStore:
    """SQL-backed persistence for the platform omnibus wallet.

    The OmnibusWallet keeps its authoritative derived state (per-user credit
    balances + lifetime deposit/withdrawal/revenue totals) in memory; this
    store snapshots that state to a single ``omnibus_state`` row on every
    movement and appends the movement to ``omnibus_entries`` for history.
    On boot the wallet calls :meth:`load_state` / :meth:`load_entries` to
    rehydrate, so user credit no longer resets to zero on a restart.

    Deals only in plain JSON-able dicts — the OmnibusEntry <-> dict mapping
    lives in the wallet, keeping this layer ignorant of the payments model."""

    def __init__(self, backend: _Backend) -> None:
        self._backend = backend

    @staticmethod
    def _get(row: Any, key: str, idx: int) -> Any:
        return row[key] if isinstance(row, dict) else row[idx]

    def append_entry(self, entry: dict) -> None:
        with self._backend:
            self._backend.execute(
                "INSERT INTO omnibus_entries"
                "(kind, amount_usdc, counterparty, actor, ts, memo, "
                "tx_hash, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(entry.get("kind", "")),
                    float(entry.get("amount_usdc", 0.0)),
                    str(entry.get("counterparty", "")),
                    str(entry.get("actor", "")),
                    float(entry.get("ts", 0.0)),
                    str(entry.get("memo", "") or ""),
                    str(entry.get("tx_hash", "") or ""),
                    json.dumps(entry.get("metadata", {})),
                ),
            )

    def load_entries(self) -> list[dict]:
        rows = self._backend.execute(
            "SELECT kind, amount_usdc, counterparty, actor, ts, memo, "
            "tx_hash, metadata FROM omnibus_entries ORDER BY seq ASC"
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            out.append({
                "kind": self._get(r, "kind", 0),
                "amount_usdc": float(self._get(r, "amount_usdc", 1)),
                "counterparty": self._get(r, "counterparty", 2),
                "actor": self._get(r, "actor", 3),
                "ts": float(self._get(r, "ts", 4)),
                "memo": self._get(r, "memo", 5) or "",
                "tx_hash": self._get(r, "tx_hash", 6) or "",
                "metadata": json.loads(self._get(r, "metadata", 7) or "{}"),
            })
        return out

    def save_state(self, snapshot: dict, *, ts: float) -> None:
        with self._backend:
            self._backend.execute(
                "INSERT INTO omnibus_state"
                "(id, user_credits, lifetime_deposits, lifetime_withdrawals, "
                "lifetime_platform_revenue, updated_ts) "
                "VALUES (1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "user_credits = excluded.user_credits, "
                "lifetime_deposits = excluded.lifetime_deposits, "
                "lifetime_withdrawals = excluded.lifetime_withdrawals, "
                "lifetime_platform_revenue = excluded.lifetime_platform_revenue, "
                "updated_ts = excluded.updated_ts",
                (
                    json.dumps(snapshot.get("user_credits", {})),
                    float(snapshot.get("lifetime_deposits", 0.0)),
                    float(snapshot.get("lifetime_withdrawals", 0.0)),
                    float(snapshot.get("lifetime_platform_revenue", 0.0)),
                    float(ts),
                ),
            )

    def load_state(self) -> dict | None:
        row = self._backend.execute(
            "SELECT user_credits, lifetime_deposits, lifetime_withdrawals, "
            "lifetime_platform_revenue FROM omnibus_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "user_credits": json.loads(self._get(row, "user_credits", 0) or "{}"),
            "lifetime_deposits": float(self._get(row, "lifetime_deposits", 1)),
            "lifetime_withdrawals": float(self._get(row, "lifetime_withdrawals", 2)),
            "lifetime_platform_revenue": float(
                self._get(row, "lifetime_platform_revenue", 3)
            ),
        }


class PersistentHarvestStore:
    """SQL-backed persistence for recurring harvest rules (doc 05).

    The HarvestScheduler keeps rules in memory and write-throughs every
    add / remove / enable-toggle / run-marker here, then reloads on boot so
    a restart doesn't drop the owner's scheduled sweeps. Deals in plain
    dicts (HarvestRule.to_json shape); the dataclass mapping lives in the
    scheduler."""

    def __init__(self, backend: _Backend) -> None:
        self._backend = backend

    @staticmethod
    def _get(row: Any, key: str, idx: int) -> Any:
        return row[key] if isinstance(row, dict) else row[idx]

    def save(self, rule: dict) -> None:
        with self._backend:
            self._backend.execute(
                "INSERT INTO harvest_rules"
                "(id, darwin_id, destination, trigger_balance_usdc, "
                "amount_usdc, interval_seconds, enabled, created_at, "
                "last_run_at, last_amount_usdc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "darwin_id = excluded.darwin_id, "
                "destination = excluded.destination, "
                "trigger_balance_usdc = excluded.trigger_balance_usdc, "
                "amount_usdc = excluded.amount_usdc, "
                "interval_seconds = excluded.interval_seconds, "
                "enabled = excluded.enabled, "
                "created_at = excluded.created_at, "
                "last_run_at = excluded.last_run_at, "
                "last_amount_usdc = excluded.last_amount_usdc",
                (
                    str(rule["id"]),
                    str(rule["darwin_id"]),
                    str(rule["destination"]),
                    float(rule["trigger_balance_usdc"]),
                    None if rule.get("amount_usdc") is None
                    else float(rule["amount_usdc"]),
                    float(rule["interval_seconds"]),
                    1 if rule.get("enabled", True) else 0,
                    float(rule["created_at"]),
                    None if rule.get("last_run_at") is None
                    else float(rule["last_run_at"]),
                    None if rule.get("last_amount_usdc") is None
                    else float(rule["last_amount_usdc"]),
                ),
            )

    def delete(self, rule_id: str) -> None:
        with self._backend:
            self._backend.execute(
                "DELETE FROM harvest_rules WHERE id = ?", (rule_id,)
            )

    def load_all(self) -> list[dict]:
        rows = self._backend.execute(
            "SELECT id, darwin_id, destination, trigger_balance_usdc, "
            "amount_usdc, interval_seconds, enabled, created_at, "
            "last_run_at, last_amount_usdc FROM harvest_rules"
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            out.append({
                "id": self._get(r, "id", 0),
                "darwin_id": self._get(r, "darwin_id", 1),
                "destination": self._get(r, "destination", 2),
                "trigger_balance_usdc": float(self._get(r, "trigger_balance_usdc", 3)),
                "amount_usdc": (
                    None if self._get(r, "amount_usdc", 4) is None
                    else float(self._get(r, "amount_usdc", 4))
                ),
                "interval_seconds": float(self._get(r, "interval_seconds", 5)),
                "enabled": bool(self._get(r, "enabled", 6)),
                "created_at": float(self._get(r, "created_at", 7)),
                "last_run_at": (
                    None if self._get(r, "last_run_at", 8) is None
                    else float(self._get(r, "last_run_at", 8))
                ),
                "last_amount_usdc": (
                    None if self._get(r, "last_amount_usdc", 9) is None
                    else float(self._get(r, "last_amount_usdc", 9))
                ),
            })
        return out


class PersistentThrottleStore:
    """SQL-backed persistence for per-Darwin resource throttles (doc 09).

    Stores only the installed caps (keyed by darwin_id+kind); the per-day
    usage counters stay in memory (they reset at midnight regardless). The
    ThrottleService write-throughs on set / clear and reloads on boot."""

    def __init__(self, backend: _Backend) -> None:
        self._backend = backend

    @staticmethod
    def _get(row: Any, key: str, idx: int) -> Any:
        return row[key] if isinstance(row, dict) else row[idx]

    def save(self, throttle: dict) -> None:
        with self._backend:
            self._backend.execute(
                "INSERT INTO resource_throttles"
                "(darwin_id, kind, max_daily_calls, installed_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(darwin_id, kind) DO UPDATE SET "
                "max_daily_calls = excluded.max_daily_calls, "
                "installed_at = excluded.installed_at",
                (
                    str(throttle["darwin_id"]),
                    str(throttle["kind"]),
                    int(throttle["max_daily_calls"]),
                    float(throttle["installed_at"]),
                ),
            )

    def delete(self, *, darwin_id: str, kind: str) -> None:
        with self._backend:
            self._backend.execute(
                "DELETE FROM resource_throttles "
                "WHERE darwin_id = ? AND kind = ?",
                (darwin_id, kind),
            )

    def delete_all(self, *, darwin_id: str) -> None:
        with self._backend:
            self._backend.execute(
                "DELETE FROM resource_throttles WHERE darwin_id = ?",
                (darwin_id,),
            )

    def load_all(self) -> list[dict]:
        rows = self._backend.execute(
            "SELECT darwin_id, kind, max_daily_calls, installed_at "
            "FROM resource_throttles"
        ).fetchall()
        return [
            {
                "darwin_id": self._get(r, "darwin_id", 0),
                "kind": self._get(r, "kind", 1),
                "max_daily_calls": int(self._get(r, "max_daily_calls", 2)),
                "installed_at": float(self._get(r, "installed_at", 3)),
            }
            for r in rows
        ]


class PersistentFiatQuoteStore:
    """SQL-backed persistence for outstanding fiat deposit quotes (doc 17).

    A fiat ramp write-throughs every quote here and deletes it on confirm,
    reloading the outstanding set on boot. This closes the
    quoted-then-restart-before-webhook gap (deposit otherwise lost) while
    keeping webhook replays idempotent (a confirmed quote is gone, so a
    replay finds nothing). Deals in FiatQuote.to_json dicts."""

    def __init__(self, backend: _Backend) -> None:
        self._backend = backend

    @staticmethod
    def _get(row: Any, key: str, idx: int) -> Any:
        return row[key] if isinstance(row, dict) else row[idx]

    def save(self, quote: dict) -> None:
        with self._backend:
            self._backend.execute(
                "INSERT INTO fiat_quotes"
                "(quote_id, provider, amount_usd, amount_usdc, fee_usd, "
                "rate, ttl_seconds, quoted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(quote_id) DO UPDATE SET "
                "provider = excluded.provider, "
                "amount_usd = excluded.amount_usd, "
                "amount_usdc = excluded.amount_usdc, "
                "fee_usd = excluded.fee_usd, "
                "rate = excluded.rate, "
                "ttl_seconds = excluded.ttl_seconds, "
                "quoted_at = excluded.quoted_at",
                (
                    str(quote["quote_id"]),
                    str(quote["provider"]),
                    float(quote["amount_usd"]),
                    float(quote["amount_usdc"]),
                    float(quote["fee_usd"]),
                    float(quote["rate"]),
                    float(quote["ttl_seconds"]),
                    float(quote["quoted_at"]),
                ),
            )

    def delete(self, quote_id: str) -> None:
        with self._backend:
            self._backend.execute(
                "DELETE FROM fiat_quotes WHERE quote_id = ?", (quote_id,)
            )

    def load_all(self) -> list[dict]:
        rows = self._backend.execute(
            "SELECT quote_id, provider, amount_usd, amount_usdc, fee_usd, "
            "rate, ttl_seconds, quoted_at FROM fiat_quotes"
        ).fetchall()
        return [
            {
                "quote_id": self._get(r, "quote_id", 0),
                "provider": self._get(r, "provider", 1),
                "amount_usd": float(self._get(r, "amount_usd", 2)),
                "amount_usdc": float(self._get(r, "amount_usdc", 3)),
                "fee_usd": float(self._get(r, "fee_usd", 4)),
                "rate": float(self._get(r, "rate", 5)),
                "ttl_seconds": float(self._get(r, "ttl_seconds", 6)),
                "quoted_at": float(self._get(r, "quoted_at", 7)),
            }
            for r in rows
        ]


class PersistentWebhookEventStore:
    """Durable idempotency ledger for payment-provider webhook events.

    ``seen`` is checked before any money effect; ``mark`` records the event
    id after processing. Keyed by the provider's event id (Stripe reuses it
    across retries), so a re-delivery is a cheap no-op — surviving restarts,
    unlike relying on quote-consumption alone."""

    def __init__(self, backend: _Backend) -> None:
        self._backend = backend

    def seen(self, event_id: str) -> bool:
        if not event_id:
            return False
        row = self._backend.execute(
            "SELECT 1 FROM processed_webhook_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return row is not None

    def mark(
        self, *, event_id: str, provider: str, status: str, ts: float,
    ) -> None:
        if not event_id:
            return
        with self._backend:
            self._backend.execute(
                "INSERT INTO processed_webhook_events"
                "(event_id, provider, status, processed_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(event_id) DO NOTHING",
                (str(event_id), str(provider), str(status), float(ts)),
            )


class PersistentLifecycleStore:
    """Backend-backed lifecycle table for runtime resume.

    Write-through on every spawn / kill / balance snapshot. The supervisor
    reads on startup (in leader mode) and re-spawns each ``alive=True``
    Darwin with the last persisted balance as its opening budget."""

    def __init__(self, backend: _Backend) -> None:
        self._backend = backend

    def upsert_spawn(
        self,
        *,
        darwin_id: str,
        opening_usdc: float,
        parent_id: str | None,
        spawned_at: float,
        escalation_auto: str | None,
    ) -> None:
        # On a fresh insert ``opening_usdc`` is the genesis treasury and is
        # the seed for ``last_balance``. On conflict — i.e. resume after
        # restart — preserve the original ``opening_usdc`` and
        # ``spawned_at``; they are historical facts, not "the value this
        # run started at." ``last_balance`` is *also* not touched on
        # conflict so the lifecycle row keeps the most recent in-flight
        # treasury snapshot (set by ``snapshot_balance``).
        with self._backend:
            self._backend.execute(
                "INSERT INTO darwin_lifecycle "
                "(darwin_id, opening_usdc, last_balance, parent_id, "
                "spawned_at, alive, killed, kill_reason, escalation_auto) "
                "VALUES (?, ?, ?, ?, ?, 1, 0, NULL, ?) "
                "ON CONFLICT(darwin_id) DO UPDATE SET "
                "parent_id = excluded.parent_id, "
                "alive = 1, killed = 0, kill_reason = NULL, "
                "escalation_auto = excluded.escalation_auto",
                (
                    darwin_id, float(opening_usdc), float(opening_usdc),
                    parent_id, float(spawned_at), escalation_auto,
                ),
            )

    def mark_killed(
        self, darwin_id: str, *, reason: str | None,
    ) -> None:
        with self._backend:
            self._backend.execute(
                "UPDATE darwin_lifecycle SET alive = 0, killed = 1, "
                "kill_reason = ? WHERE darwin_id = ?",
                (reason, darwin_id),
            )

    def snapshot_balance(
        self, darwin_id: str, *, balance_usdc: float,
    ) -> None:
        with self._backend:
            self._backend.execute(
                "UPDATE darwin_lifecycle SET last_balance = ? "
                "WHERE darwin_id = ?",
                (float(balance_usdc), darwin_id),
            )

    def set_quarantine(
        self,
        darwin_id: str,
        *,
        active: bool,
        reason: str | None = None,
        operator: str | None = None,
        applied_ts: float | None = None,
    ) -> None:
        """Persist the doc-09 level-4 quarantine state (doc 23). Write-through
        on every quarantine / unquarantine so a supervisor restart restores
        a quarantined Darwin still quarantined, not silently running."""
        with self._backend:
            self._backend.execute(
                "UPDATE darwin_lifecycle SET quarantined = ?, "
                "quarantine_reason = ?, quarantine_operator = ?, "
                "quarantine_applied_ts = ? WHERE darwin_id = ?",
                (
                    1 if active else 0,
                    reason if active else None,
                    operator if active else None,
                    applied_ts if active else None,
                    darwin_id,
                ),
            )

    def get(self, darwin_id: str) -> LifecycleRecord | None:
        row = self._backend.execute(
            "SELECT darwin_id, opening_usdc, last_balance, parent_id, "
            "spawned_at, alive, killed, kill_reason, escalation_auto, "
            "quarantined, quarantine_reason, quarantine_operator, "
            "quarantine_applied_ts "
            "FROM darwin_lifecycle WHERE darwin_id = ?",
            (darwin_id,),
        ).fetchone()
        if row is None:
            return None
        return _lifecycle_from_row(row)

    def list_alive(self) -> list[LifecycleRecord]:
        rows = list(self._backend.execute(
            "SELECT darwin_id, opening_usdc, last_balance, parent_id, "
            "spawned_at, alive, killed, kill_reason, escalation_auto, "
            "quarantined, quarantine_reason, quarantine_operator, "
            "quarantine_applied_ts "
            "FROM darwin_lifecycle WHERE alive = 1 ORDER BY spawned_at ASC"
        ).fetchall())
        return [_lifecycle_from_row(r) for r in rows]

    def list_all(self) -> list[LifecycleRecord]:
        rows = list(self._backend.execute(
            "SELECT darwin_id, opening_usdc, last_balance, parent_id, "
            "spawned_at, alive, killed, kill_reason, escalation_auto, "
            "quarantined, quarantine_reason, quarantine_operator, "
            "quarantine_applied_ts "
            "FROM darwin_lifecycle ORDER BY spawned_at ASC"
        ).fetchall())
        return [_lifecycle_from_row(r) for r in rows]


def _lifecycle_from_row(row) -> LifecycleRecord:
    def _get(key, default=None):
        try:
            return row[key]
        except (KeyError, IndexError):
            return default
    return LifecycleRecord(
        darwin_id=_get("darwin_id"),
        opening_usdc=float(_get("opening_usdc") or 0.0),
        last_balance=(
            float(_get("last_balance")) if _get("last_balance") is not None else None
        ),
        parent_id=_get("parent_id"),
        spawned_at=float(_get("spawned_at") or 0.0),
        alive=bool(_get("alive")),
        killed=bool(_get("killed")),
        kill_reason=_get("kill_reason"),
        escalation_auto=_get("escalation_auto"),
        quarantined=bool(_get("quarantined")),
        quarantine_reason=_get("quarantine_reason"),
        quarantine_operator=_get("quarantine_operator"),
        quarantine_applied_ts=(
            float(_get("quarantine_applied_ts"))
            if _get("quarantine_applied_ts") is not None
            else None
        ),
    )


# -- per-Darwin audit sink (doc 08 §HA: lossless resume) -------------------
class PersistentDarwinAuditSink:
    """Persistent variant of :class:`darwin_fakes.fake_platform.MemoryAuditSink`.

    Records survive process restart; ``read_all`` returns them in seq order
    so the genome's :class:`darwin_dna.machinery.audit_logger.AuditLogger`
    rebuilds its chain head from the same prefix it saw before the restart.
    This is the cornerstone of lossless resume: the same Darwin id picks
    up exactly where its previous life left off.

    Concurrency note: AuditLogger already serializes access via its own
    lock; this sink doesn't need its own.
    """

    def __init__(self, backend: _Backend, *, darwin_id: str) -> None:
        self._backend = backend
        self._darwin_id = darwin_id
        # Track the next seq we expect so duplicate inserts (e.g. a retry
        # after a transient DB hiccup) are recognized and skipped rather
        # than re-inserted with a PK conflict.
        self._next_seq = self._max_seq() + 1

    def _max_seq(self) -> int:
        row = self._backend.execute(
            "SELECT MAX(seq) AS m FROM darwin_audit_records "
            "WHERE darwin_id = ?",
            (self._darwin_id,),
        ).fetchone()
        if row is None:
            return -1
        m = row["m"] if isinstance(row, dict) else row[0]
        return -1 if m is None else int(m)

    def read_all(self) -> list[dict]:
        rows = list(self._backend.execute(
            "SELECT seq, record FROM darwin_audit_records "
            "WHERE darwin_id = ? ORDER BY seq ASC",
            (self._darwin_id,),
        ).fetchall())
        out: list[dict] = []
        for r in rows:
            rec_json = r["record"] if isinstance(r, dict) else r[1]
            out.append(json.loads(rec_json))
        return out

    def append(self, record: dict) -> None:
        seq = int(record.get("seq", self._next_seq))
        with self._backend:
            self._backend.execute(
                "INSERT INTO darwin_audit_records "
                "(darwin_id, seq, record, ts) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(darwin_id, seq) DO NOTHING",
                (
                    self._darwin_id, seq, json.dumps(record),
                    float(record.get("ts", 0.0)),
                ),
            )
        self._next_seq = max(self._next_seq, seq + 1)


# -- platform-wide checkpoint store (doc 03/12) ----------------------------
class PersistentCheckpointStore:
    """Persistent variant of :class:`MemoryCheckpointStore`. Used by the
    genome's evolution loop via ``platform.checkpoint.save / load``.

    One row per Darwin (the latest checkpoint). The genome's boot path
    reads the row at startup; the drive loop overwrites it at every node
    boundary. Restart-survivable so a leader failover resumes from the
    last saved node, not from a fresh state."""

    def __init__(self, backend: _Backend) -> None:
        self._backend = backend
        self.saves = 0

    @staticmethod
    def _json_default(obj):
        # The genome's OrchestratorState.model_dump() may include
        # frozensets (e.g. required_tags). JSON doesn't natively
        # support them; encode as sorted lists. tuples + sets fall
        # through the same path.
        if isinstance(obj, (frozenset, set)):
            return sorted(obj)
        raise TypeError(f"unserialisable type {type(obj).__name__}")

    def save(self, darwin_id: str, node: str, state: dict) -> None:
        payload = json.dumps(state, default=self._json_default)
        with self._backend:
            self._backend.execute(
                "INSERT INTO darwin_checkpoints "
                "(darwin_id, node, state, saved_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(darwin_id) DO UPDATE SET "
                "node = excluded.node, state = excluded.state, "
                "saved_at = excluded.saved_at",
                (darwin_id, node, payload, float(time.time())),
            )
        self.saves += 1

    def load(self, darwin_id: str) -> dict | None:
        row = self._backend.execute(
            "SELECT node, state FROM darwin_checkpoints "
            "WHERE darwin_id = ?",
            (darwin_id,),
        ).fetchone()
        if row is None:
            return None
        node = row["node"] if isinstance(row, dict) else row[0]
        state_json = row["state"] if isinstance(row, dict) else row[1]
        return {"node": node, "state": json.loads(state_json)}


# -- per-Darwin manifest store (doc 08 §HA: lossless resume) ---------------
class PersistentManifestStore:
    """Stores the sealed manifest + layer blobs per Darwin so a resume
    can re-mint the exact same DNA identity instead of generating a
    fresh genesis. The blob bytes are base64-encoded for the TEXT column.

    The audit chain is the authoritative life record; the manifest is
    what lets ``boot()`` verify the chain belongs to the same Darwin."""

    def __init__(self, backend: _Backend) -> None:
        self._backend = backend

    def put(
        self,
        *,
        darwin_id: str,
        manifest_json: str,
        layer_blobs: dict[int, bytes],
        layer_names: dict[int, str],
    ) -> None:
        import base64
        blobs_b64 = {
            str(k): base64.b64encode(v).decode("ascii")
            for k, v in layer_blobs.items()
        }
        names = {str(k): v for k, v in layer_names.items()}
        with self._backend:
            self._backend.execute(
                "INSERT INTO darwin_manifests "
                "(darwin_id, manifest, layer_blobs, layer_names, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(darwin_id) DO UPDATE SET "
                "manifest = excluded.manifest, "
                "layer_blobs = excluded.layer_blobs, "
                "layer_names = excluded.layer_names",
                (
                    darwin_id, manifest_json, json.dumps(blobs_b64),
                    json.dumps(names), float(time.time()),
                ),
            )

    def get(
        self, darwin_id: str,
    ) -> tuple[str, dict[int, bytes], dict[int, str]] | None:
        import base64
        row = self._backend.execute(
            "SELECT manifest, layer_blobs, layer_names "
            "FROM darwin_manifests WHERE darwin_id = ?",
            (darwin_id,),
        ).fetchone()
        if row is None:
            return None
        manifest_json = row["manifest"] if isinstance(row, dict) else row[0]
        blobs_json = row["layer_blobs"] if isinstance(row, dict) else row[1]
        names_json = row["layer_names"] if isinstance(row, dict) else row[2]
        blobs = {
            int(k): base64.b64decode(v)
            for k, v in json.loads(blobs_json).items()
        }
        names = {int(k): v for k, v in json.loads(names_json).items()}
        return manifest_json, blobs, names


# -- migration registry ----------------------------------------------------
# Each entry maps an INCOMING version number → a function that takes a
# _Backend and applies that step's schema changes. Additive table
# creations are already covered by the CREATE TABLE IF NOT EXISTS in
# the main schema; this registry is for changes that those statements
# CAN'T express (column drops, type changes, data migrations).
#
# Version 1 is the baseline — every table the schema currently
# declares. There's no migration for it; the version stamp is just
# recorded.
# Version 2 adds platform_retired_keys (additive — handled by the
# schema's CREATE TABLE IF NOT EXISTS).
# Version 3 adds the quarantine_* columns to darwin_lifecycle. Adding a
# COLUMN to an *existing* table is NOT covered by CREATE TABLE IF NOT
# EXISTS, so this step needs a real migration.


def _column_exists(backend: _Backend, table: str, column: str) -> bool:
    """True if ``table`` already has ``column``. Lets an add-column
    migration stay idempotent — a freshly created DB already carries the
    column from the main DDL, so the migration must not re-add it."""
    if backend.kind == "postgres":
        row = backend.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = ? AND column_name = ?",
            (table, column),
        ).fetchone()
        return row is not None
    rows = backend.execute(f"PRAGMA table_info({table})").fetchall()
    names: set[str] = set()
    for r in rows:
        try:
            names.add(r["name"])
        except (KeyError, IndexError, TypeError):
            names.add(r[1])  # sqlite PRAGMA: (cid, name, type, ...)
    return column in names


def _migrate_v3_quarantine_columns(backend: _Backend) -> None:
    """v3: persist quarantine state on darwin_lifecycle (doc 23) so a
    quarantined Darwin survives a supervisor restart still quarantined."""
    real = "DOUBLE PRECISION" if backend.kind == "postgres" else "REAL"
    columns = (
        ("quarantined", "INTEGER NOT NULL DEFAULT 0"),
        ("quarantine_reason", "TEXT"),
        ("quarantine_operator", "TEXT"),
        ("quarantine_applied_ts", real),
    )
    for name, coldef in columns:
        if not _column_exists(backend, "darwin_lifecycle", name):
            backend.execute(
                f"ALTER TABLE darwin_lifecycle ADD COLUMN {name} {coldef}"
            )


_MIGRATIONS: dict[int, Any] = {
    # 1: no-op baseline (covered by CREATE TABLE IF NOT EXISTS).
    # 2: no-op (platform_retired_keys table covered by schema).
    3: _migrate_v3_quarantine_columns,
    # 4: no-op (darwin_preapprovals table covered by schema).
    # 5: no-op (freelancer_registry table covered by schema).
    6: lambda backend: _migrate_v6_preapproval_use_counter(backend),
    # 7-10: no-op slots reserved for the omnibus / harvest / throttle /
    # signing-key migrations the schema already covers with CREATE TABLE
    # IF NOT EXISTS.
    # 11: push_subscriptions table — covered by CREATE TABLE IF NOT EXISTS,
    # so no ALTER needed; the bump just ensures opened DBs from older code
    # get the version row updated.
}


def _migrate_v6_preapproval_use_counter(backend: _Backend) -> None:
    """v6: add max_uses + used_count to darwin_preapprovals so a max-uses
    pre-approval rule can be enforced and consumes survive restart."""
    for col, default in (
        ("max_uses", ""),
        ("used_count", "DEFAULT 0"),
    ):
        if not _column_exists(backend, "darwin_preapprovals", col):
            coldef = "INTEGER " + (default or "")
            if col == "used_count":
                coldef = "INTEGER NOT NULL DEFAULT 0"
            backend.execute(
                f"ALTER TABLE darwin_preapprovals ADD COLUMN {col} {coldef}"
            )
