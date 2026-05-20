"""Tests for runtime resume after a supervisor restart (doc 08 §HA).

These exercise the persistent lifecycle table + the Supervisor's
``ensure_leader_started`` path that re-spawns each alive Darwin.

We model a "process restart" by closing the first Supervisor and opening
a second against the same DB file. The audit chain + lineage edges +
billing carry across; the runtime is fresh."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from darwin_platform.supervisor import Supervisor


@pytest.mark.asyncio
async def test_lifecycle_persisted_on_spawn(tmp_path):
    db = tmp_path / "platform.db"
    sup = Supervisor(persistence_path=str(db))
    status = await sup.spawn(opening_usdc=100.0)
    did = status.darwin_id
    # Check the persistent lifecycle store recorded the spawn.
    rec = sup._lifecycle.get(did)
    assert rec is not None
    assert rec.alive is True
    assert rec.killed is False
    assert rec.opening_usdc == 100.0
    await sup.shutdown(kill_running=False)
    sup._persistence.close()


@pytest.mark.asyncio
async def test_lifecycle_marks_kill(tmp_path):
    db = tmp_path / "platform.db"
    sup = Supervisor(persistence_path=str(db))
    status = await sup.spawn(opening_usdc=100.0)
    await sup.kill(status.darwin_id, reason="test-kill")
    rec = sup._lifecycle.get(status.darwin_id)
    assert rec.alive is False
    assert rec.killed is True
    assert rec.kill_reason == "test-kill"
    await sup.shutdown(kill_running=False)
    sup._persistence.close()


@pytest.mark.asyncio
async def test_resume_re_spawns_alive_darwins(tmp_path):
    """Spawn a Darwin, close the supervisor, open a fresh one against
    the same DB. The fresh supervisor's ``ensure_leader_started`` should
    re-spawn the alive Darwin."""
    db = tmp_path / "platform.db"
    sup_a = Supervisor(persistence_path=str(db))
    await sup_a.spawn(opening_usdc=50.0)
    await sup_a.spawn(opening_usdc=75.0)
    await sup_a.shutdown(kill_running=False)
    sup_a._persistence.close()

    # Fresh supervisor — same DB.
    sup_b = Supervisor(persistence_path=str(db))
    # ensure_leader_started runs the resume.
    await sup_b.ensure_leader_started()
    # Two new running Darwins (with fresh ids; the audit log records
    # the link via the resume entries).
    assert len(sup_b._darwins) == 2
    resume_entries = sup_b._audit.entries(action="resume")
    assert len(resume_entries) == 2
    await sup_b.shutdown(kill_running=False)
    sup_b._persistence.close()


@pytest.mark.asyncio
async def test_resume_skips_killed_darwins(tmp_path):
    db = tmp_path / "platform.db"
    sup_a = Supervisor(persistence_path=str(db))
    status1 = await sup_a.spawn(opening_usdc=50.0)
    status2 = await sup_a.spawn(opening_usdc=75.0)
    await sup_a.kill(status1.darwin_id, reason="dev-shutdown")
    await sup_a.shutdown(kill_running=False)
    sup_a._persistence.close()

    sup_b = Supervisor(persistence_path=str(db))
    await sup_b.ensure_leader_started()
    # Only the unkilled Darwin is re-spawned.
    assert len(sup_b._darwins) == 1
    await sup_b.shutdown(kill_running=False)
    sup_b._persistence.close()


@pytest.mark.asyncio
async def test_resume_idempotent(tmp_path):
    """Calling ensure_leader_started twice does not double-resume."""
    db = tmp_path / "platform.db"
    sup_a = Supervisor(persistence_path=str(db))
    await sup_a.spawn(opening_usdc=50.0)
    await sup_a.shutdown(kill_running=False)
    sup_a._persistence.close()

    sup_b = Supervisor(persistence_path=str(db))
    await sup_b.ensure_leader_started()
    n1 = len(sup_b._darwins)
    # The persisted Darwin is now ALSO recorded under the new id; calling
    # ensure_leader_started again should be a no-op.
    await sup_b.ensure_leader_started()
    assert len(sup_b._darwins) == n1
    await sup_b.shutdown(kill_running=False)
    sup_b._persistence.close()


@pytest.mark.asyncio
async def test_no_persistence_means_no_lifecycle(tmp_path):
    """Without persistence, the lifecycle store is None and resume is
    a no-op (which is the right behaviour for ephemeral dev)."""
    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    assert sup._lifecycle is None
    await sup.shutdown(kill_running=True)
