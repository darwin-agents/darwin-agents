"""Dead-Darwin data retention (doc 09 commitment #3).

Killing a Darwin must NOT immediately destroy its namespace + organism DB —
the data survives the retention window so the owner can inspect it and a
re-spawn can inherit accounts/databases. Teardown is deferred to a reaper that
only fires once the window elapses (or an operator force-tears-down).
"""

from __future__ import annotations

import asyncio

import pytest

from darwin_platform.runtime.supervisor import Supervisor, SupervisorError
from darwin_platform.tenancy.namespace_provisioner import (
    NamespaceProvisioner,
    StubK8sBackend,
    StubOrganismDbBackend,
    StubVaultNamespaceBackend,
    TenantNamespaceStatus,
)


def _provisioner():
    return NamespaceProvisioner(
        k8s=StubK8sBackend(),
        vault=StubVaultNamespaceBackend(),
        organism_db=StubOrganismDbBackend(),
    )


# ---------------------------------------------------------------------------
# provisioner: schedule vs destroy
# ---------------------------------------------------------------------------

def test_schedule_teardown_retains_then_due():
    p = _provisioner()
    p.provision(darwin_id="d1", actor="t")
    p.schedule_teardown(darwin_id="d1", due_ts=1000.0)
    t = p.get("d1")
    assert t is not None  # retained
    assert t.status == TenantNamespaceStatus.DECEASED
    assert t.teardown_due_ts == 1000.0
    # Not yet due.
    assert p.due_for_teardown(now=999.0) == []
    # Due once the window elapses.
    assert p.due_for_teardown(now=1000.0) == ["d1"]
    # The destructive teardown still works + clears the cache.
    p.teardown(darwin_id="d1")
    assert p.get("d1") is None


# ---------------------------------------------------------------------------
# supervisor: kill defers, reaper destroys
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kill_retains_data_until_window_elapses():
    prov = _provisioner()
    sup = Supervisor(
        heartbeat_seconds=0.001, maintenance_interval_seconds=0.05,
        namespace_provisioner=prov,
        dead_darwin_retention_seconds=10_000.0,  # long window
    )
    status = await sup.spawn(opening_usdc=100.0, actor="owner")
    did = status.darwin_id
    assert prov.get(did) is not None  # provisioned on spawn
    try:
        await sup.kill(did, reason="owner kill")
        t = prov.get(did)
        assert t is not None  # NOT destroyed
        assert t.status == TenantNamespaceStatus.DECEASED
        assert t.teardown_due_ts is not None
        # Several maintenance cycles pass; window hasn't elapsed → still here.
        await asyncio.sleep(0.25)
        assert prov.get(did) is not None
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_reaper_destroys_after_window():
    prov = _provisioner()
    sup = Supervisor(
        heartbeat_seconds=0.001, maintenance_interval_seconds=0.05,
        namespace_provisioner=prov,
        dead_darwin_retention_seconds=0.0,  # due immediately
    )
    # Keep one Darwin alive: fleet maintenance (incl. the reaper) is driven by
    # a living Darwin's heartbeat cadence.
    keeper = await sup.spawn(opening_usdc=100.0, actor="owner")
    victim = await sup.spawn(opening_usdc=100.0, actor="owner")
    did = victim.darwin_id
    try:
        await sup.kill(did, reason="owner kill")
        # Maintenance (driven by the keeper) reaps the now-due dead Darwin.
        await asyncio.sleep(0.3)
        assert prov.get(did) is None  # reaped
        assert sup.audit_log.entries(action="dead_darwin_reaped")
        # The keeper's namespace is untouched.
        assert prov.get(keeper.darwin_id) is not None
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_operator_force_teardown():
    prov = _provisioner()
    sup = Supervisor(
        heartbeat_seconds=0.001, namespace_provisioner=prov,
        dead_darwin_retention_seconds=10_000.0,
    )
    status = await sup.spawn(opening_usdc=100.0, actor="owner")
    did = status.darwin_id
    try:
        await sup.kill(did)
        assert prov.get(did) is not None  # retained
        # Owner explicitly requests deletion before the window elapses.
        sup.teardown_dead_darwin(did, actor="owner")
        assert prov.get(did) is None
        assert sup.audit_log.entries(action="teardown_dead_darwin")
    finally:
        await sup.shutdown(kill_running=True)
