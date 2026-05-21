"""HA leader election for the supervisor.

When the platform runs as multiple replicas (the Postgres-backed
persistence port enables this), exactly one instance is the *leader* —
it runs the per-Darwin drive loops, observes child spawns, polls the
escalation inbox, runs the monitor. The others are *followers* — they
hold the same persisted state read-through but don't tick. On leader
loss (pod replacement, crash, network partition), a follower wins the
election within one heartbeat and takes over. Followers' ``/readyz`` is
503 until they become leader so the k8s Service routes API traffic only
to the leader.

Why advisory locks
------------------

Postgres ``pg_advisory_lock`` is a session-scoped lock keyed on a
``BIGINT``. The lock is held while the locking connection stays open;
**the moment the connection drops, the lock auto-releases**. That gives
us the failure-detection mechanism for free — no separate heartbeat
table to age out, no clock-skew TTL to tune.

This module ships:

- :class:`LeaderElection` — the Protocol the supervisor calls.
- :class:`NoLeaderElection` — the default. Always reports leadership.
  Right for single-replica deploys (SQLite mode, or Postgres mode with
  ``replicaCount: 1``).
- :class:`PostgresAdvisoryLockElection` — opens a dedicated psycopg
  connection, holds ``pg_try_advisory_lock`` on a configurable BIGINT
  key, polls leadership every ``heartbeat_interval`` seconds. On lock
  loss (connection dropped, or another instance got it first) the
  follower polls forever until it wins. Idempotent: calling
  :meth:`start` twice is a no-op.

The lock key defaults to a hash of ``"darwin-platform/supervisor"`` so
distinct chart releases on the same Postgres don't fight unless an
operator intentionally aligns them.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
import time
from typing import Protocol, runtime_checkable


def _default_lock_key() -> int:
    """A deterministic 63-bit signed integer derived from a fixed label.
    Postgres advisory locks accept any BIGINT (signed 64-bit); we cap to
    63 bits so the value fits comfortably in any client representation."""
    digest = hashlib.sha256(b"darwin-platform/supervisor/leader").digest()
    n = struct.unpack(">Q", digest[:8])[0]
    return n & 0x7FFF_FFFF_FFFF_FFFF


DEFAULT_LOCK_KEY = _default_lock_key()


@runtime_checkable
class LeaderElection(Protocol):
    """The supervisor's view of the leadership service. ``start`` /
    ``stop`` are async because real impls run background tasks; the
    no-op default impl just ignores them."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    @property
    def is_leader(self) -> bool: ...


class NoLeaderElection:
    """Single-replica default. Always leader; no I/O. Right when running
    against SQLite or when only one supervisor pod is in the deployment."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    @property
    def is_leader(self) -> bool:
        return True


class PostgresAdvisoryLockElection:
    """Acquires + holds ``pg_advisory_lock(lock_key)`` on a dedicated
    psycopg connection. While the connection lives, this instance is
    leader; on disconnect (pod death, network partition), the lock
    auto-releases and any waiting follower wins on its next poll.

    ``heartbeat_interval`` controls how often a non-leader polls; a
    leader pings with ``SELECT 1`` at the same cadence to keep the
    connection healthy.
    """

    def __init__(
        self,
        dsn: str,
        *,
        lock_key: int = DEFAULT_LOCK_KEY,
        heartbeat_interval: float = 5.0,
    ) -> None:
        try:
            import psycopg  # noqa: F401 — imported to surface ImportError early
        except ImportError as exc:  # pragma: no cover - install hint
            raise ImportError(
                "PostgresAdvisoryLockElection requires the [postgres] "
                "extra: `pip install darwin-dna[postgres]`"
            ) from exc
        if not dsn.startswith(("postgres://", "postgresql://")):
            raise ValueError(
                "PostgresAdvisoryLockElection needs a postgres:// DSN"
            )
        self._dsn = dsn
        self._lock_key = int(lock_key)
        self._heartbeat = float(heartbeat_interval)
        self._conn = None
        self._is_leader = False
        self._task: asyncio.Task | None = None
        self._stopping = False

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def start(self) -> None:
        if self._task is not None:
            return  # idempotent
        self._stopping = False
        # Initial synchronous attempt so callers see ``is_leader`` correct
        # on return rather than racing the first heartbeat tick.
        self._try_acquire()
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._release()

    # -- internals ---------------------------------------------------------
    def _try_acquire(self) -> bool:
        """Try once to become leader. Returns True if we hold the lock
        after the call; False otherwise. Idempotent: calling while
        already leader keeps the existing connection."""
        if self._is_leader and self._conn is not None and not self._conn.closed:
            return True
        import psycopg

        # Ensure no stale connection from a previous attempt.
        self._release()
        try:
            conn = psycopg.connect(self._dsn, autocommit=True)
        except Exception:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s)", (self._lock_key,)
                )
                row = cur.fetchone()
                acquired = bool(row[0]) if row else False
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            return False
        if not acquired:
            try:
                conn.close()
            except Exception:
                pass
            return False
        # Hold the lock by keeping the connection open.
        self._conn = conn
        self._is_leader = True
        return True

    def _release(self) -> None:
        """Drop the connection; Postgres auto-releases the lock."""
        self._is_leader = False
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    async def _heartbeat_loop(self) -> None:
        """Periodically: if leader, ping the connection to detect drops;
        if follower, try to acquire. The cadence is the only knob — too
        short wastes a backend connection per pod; too long widens the
        failover window."""
        while not self._stopping:
            try:
                await asyncio.sleep(self._heartbeat)
            except asyncio.CancelledError:
                break
            if self._stopping:
                break
            if self._is_leader and self._conn is not None:
                # Cheap connection-liveness check. If the conn is dead,
                # close it and become a candidate again on the next tick.
                try:
                    with self._conn.cursor() as cur:
                        cur.execute("SELECT 1")
                except Exception:
                    self._release()
            else:
                # Follower: try to win the lock.
                self._try_acquire()
