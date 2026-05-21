"""HA leader election (doc 08).

Mix of unit + integration tests:

- ``NoLeaderElection`` always reports leader and is the default.
- ``PostgresAdvisoryLockElection`` (gated by ``DARWIN_POSTGRES_TEST_DSN``)
  acquires a real ``pg_try_advisory_lock``; a second instance on the
  same key gets False; releasing the first lets the second win.
- Supervisor integration: with HA on, only the leader's spawn drives
  Darwins; the follower's mutating endpoints raise SupervisorError.
- HTTP ``/readyz``: 200 on leader, 503 on follower.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from darwin_platform import (
    NoLeaderElection,
    Supervisor,
    SupervisorError,
)

DSN = os.environ.get("DARWIN_POSTGRES_TEST_DSN")
PG_AVAILABLE = bool(DSN)


# -- NoLeaderElection -----------------------------------------------------
async def test_no_leader_election_is_default_and_always_leader():
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        assert sup.is_leader is True
        # ensure_leader_started is idempotent + non-blocking for the no-op.
        await sup.ensure_leader_started()
        await sup.ensure_leader_started()
        assert sup.is_leader is True
    finally:
        await sup.shutdown()


async def test_no_leader_election_explicit_construction():
    sup = Supervisor(
        heartbeat_seconds=0.0,
        leader_election=NoLeaderElection(),
    )
    try:
        assert sup.is_leader is True
    finally:
        await sup.shutdown()


# -- PostgresAdvisoryLockElection ----------------------------------------
@pytest.mark.skipif(
    not PG_AVAILABLE,
    reason="DARWIN_POSTGRES_TEST_DSN not set",
)
async def test_postgres_lock_first_wins_second_blocks():
    from darwin_platform import PostgresAdvisoryLockElection

    # Same lock key on both — only the first can hold it.
    key = 12345
    a = PostgresAdvisoryLockElection(DSN, lock_key=key, heartbeat_interval=60)
    b = PostgresAdvisoryLockElection(DSN, lock_key=key, heartbeat_interval=60)
    try:
        await a.start()
        await b.start()
        # The race is decided in start()'s synchronous _try_acquire; one
        # of them MUST be leader, the other MUST NOT.
        assert a.is_leader != b.is_leader
        if a.is_leader:
            leader, follower = a, b
        else:
            leader, follower = b, a
        assert leader.is_leader and not follower.is_leader

        # Release the leader → follower can take over after a polling cycle.
        await leader.stop()
        # Follower's heartbeat default is 60s; force a manual acquire so
        # the test doesn't hang.
        # Calling the private hook directly is acceptable for tests of
        # this class — it exists to make the polling testable.
        follower._try_acquire()  # noqa: SLF001
        assert follower.is_leader is True
    finally:
        try:
            await a.stop()
        except Exception:
            pass
        try:
            await b.stop()
        except Exception:
            pass


@pytest.mark.skipif(
    not PG_AVAILABLE,
    reason="DARWIN_POSTGRES_TEST_DSN not set",
)
async def test_postgres_lock_rejects_sqlite_dsn():
    from darwin_platform import PostgresAdvisoryLockElection

    with pytest.raises(ValueError, match="postgres://"):
        PostgresAdvisoryLockElection("sqlite:///foo.db")


@pytest.mark.skipif(
    not PG_AVAILABLE,
    reason="DARWIN_POSTGRES_TEST_DSN not set",
)
async def test_supervisor_with_HA_only_leader_can_spawn():
    """Two supervisors share a Postgres + a lock key. Only the leader's
    spawn works; the follower refuses with SupervisorError."""
    from darwin_platform import PostgresAdvisoryLockElection

    key = 67890
    elector_a = PostgresAdvisoryLockElection(DSN, lock_key=key)
    elector_b = PostgresAdvisoryLockElection(DSN, lock_key=key)
    sup_a = Supervisor(
        heartbeat_seconds=0.0, persistence_path=DSN,
        leader_election=elector_a,
    )
    sup_b = Supervisor(
        heartbeat_seconds=0.0, persistence_path=DSN,
        leader_election=elector_b,
    )
    try:
        await sup_a.ensure_leader_started()
        await sup_b.ensure_leader_started()
        assert sup_a.is_leader != sup_b.is_leader
        leader, follower = (
            (sup_a, sup_b) if sup_a.is_leader else (sup_b, sup_a)
        )
        # Leader can spawn.
        status = await leader.spawn(opening_usdc=80.0, actor="alice")
        assert status.darwin_id
        # Follower refuses with a clear error.
        with pytest.raises(SupervisorError, match="not the current leader"):
            await follower.spawn(opening_usdc=80.0, actor="bob")
    finally:
        await sup_a.shutdown()
        await sup_b.shutdown()


@pytest.mark.skipif(
    not PG_AVAILABLE,
    reason="DARWIN_POSTGRES_TEST_DSN not set",
)
async def test_supervisor_leader_handoff_after_shutdown():
    """When the leader shuts down, the follower's poll picks up the lock
    on the next heartbeat — within roughly the heartbeat interval."""
    from darwin_platform import PostgresAdvisoryLockElection

    key = 11111
    elector_a = PostgresAdvisoryLockElection(
        DSN, lock_key=key, heartbeat_interval=0.5
    )
    elector_b = PostgresAdvisoryLockElection(
        DSN, lock_key=key, heartbeat_interval=0.5
    )
    sup_a = Supervisor(
        heartbeat_seconds=0.0, persistence_path=DSN,
        leader_election=elector_a,
    )
    sup_b = Supervisor(
        heartbeat_seconds=0.0, persistence_path=DSN,
        leader_election=elector_b,
    )
    try:
        await sup_a.ensure_leader_started()
        await sup_b.ensure_leader_started()
        leader = sup_a if sup_a.is_leader else sup_b
        follower = sup_b if leader is sup_a else sup_a

        # Trigger handoff: shut down the leader. The follower's
        # heartbeat task tries to acquire on the next tick.
        await leader.shutdown()
        # Wait up to 5 heartbeat intervals.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not follower.is_leader:
            await asyncio.sleep(0.2)
        assert follower.is_leader, "follower never won the lock"
    finally:
        try:
            await sup_a.shutdown()
        except Exception:
            pass
        try:
            await sup_b.shutdown()
        except Exception:
            pass


# -- /readyz HTTP behaviour ----------------------------------------------
async def test_readyz_200_for_default_no_election():
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from darwin_platform.api import build_app

    sup = Supervisor(heartbeat_seconds=0.0)
    app = build_app(sup)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t"
    ) as c:
        try:
            r = await c.get("/readyz")
            assert r.status_code == 200
            body = r.json()
            assert body["leader"] is True
        finally:
            await sup.shutdown()


@pytest.mark.skipif(
    not PG_AVAILABLE,
    reason="DARWIN_POSTGRES_TEST_DSN not set",
)
async def test_readyz_503_on_follower():
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from darwin_platform import PostgresAdvisoryLockElection
    from darwin_platform.api import build_app

    key = 22222
    elector_leader = PostgresAdvisoryLockElection(DSN, lock_key=key)
    elector_follower = PostgresAdvisoryLockElection(DSN, lock_key=key)
    sup_leader = Supervisor(
        heartbeat_seconds=0.0, persistence_path=DSN,
        leader_election=elector_leader,
    )
    sup_follower = Supervisor(
        heartbeat_seconds=0.0, persistence_path=DSN,
        leader_election=elector_follower,
    )
    # Force the lock onto the first supervisor so the second is
    # deterministically the follower.
    await sup_leader.ensure_leader_started()
    await sup_follower.ensure_leader_started()
    if not sup_leader.is_leader:
        sup_leader, sup_follower = sup_follower, sup_leader
    assert sup_leader.is_leader and not sup_follower.is_leader

    app_follower = build_app(sup_follower)
    transport = httpx.ASGITransport(app=app_follower)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t"
    ) as c:
        try:
            r = await c.get("/readyz")
            assert r.status_code == 503
            assert r.json()["leader"] is False
            # Liveness still 200 — the pod is alive, just not the leader.
            r = await c.get("/healthz")
            assert r.status_code == 200
        finally:
            await sup_leader.shutdown()
            await sup_follower.shutdown()
