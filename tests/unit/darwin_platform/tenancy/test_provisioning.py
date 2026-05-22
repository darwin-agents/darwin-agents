"""Tests for the platform-mediated provisioning service (docs/engineering/
04, 07). Covers the service in isolation, the per-Darwin scoped port, the
supervisor wiring, the quota gates, and the HTTP routes."""

from __future__ import annotations


import pytest

from darwin_platform.tenancy.provisioning import (
    ProvisioningError,
    ProvisioningQuotas,
    ProvisioningService,
    ScopedProvisionPort,
)


# ---------------------------------------------------------------------------
# 1. ProvisioningService core
# ---------------------------------------------------------------------------

def test_provision_returns_record_and_payload():
    svc = ProvisioningService()
    record, payload = svc.provision(
        darwin_id="d-1", kind="postgres", spec={"size": "small"},
    )
    assert record.kind == "postgres"
    assert record.status == "active"
    assert record.darwin_id == "d-1"
    assert payload["id"].startswith("postgres-")
    assert payload["creds"].startswith("stub-creds:")
    # creds_ref is the vault path, not the secret itself
    assert "darwins/d-1/scoped/infrastructure" in record.creds_ref


def test_list_resources_filters_deprovisioned():
    svc = ProvisioningService()
    r1, _ = svc.provision(darwin_id="d-1", kind="postgres", spec={})
    r2, _ = svc.provision(darwin_id="d-1", kind="bucket", spec={})
    svc.deprovision(darwin_id="d-1", resource_id=r1.id)

    active = svc.list_resources("d-1")
    assert len(active) == 1
    assert active[0].id == r2.id

    all_rows = svc.list_resources("d-1", include_deprovisioned=True)
    assert len(all_rows) == 2


def test_deprovision_returns_updated_record():
    svc = ProvisioningService()
    r, _ = svc.provision(darwin_id="d-1", kind="postgres", spec={})
    updated = svc.deprovision(darwin_id="d-1", resource_id=r.id)
    assert updated.status == "deprovisioned"
    assert updated.deprovisioned_at is not None


def test_deprovision_unknown_raises():
    svc = ProvisioningService()
    with pytest.raises(ProvisioningError):
        svc.deprovision(darwin_id="d-1", resource_id="postgres-99")


def test_double_deprovision_raises():
    svc = ProvisioningService()
    r, _ = svc.provision(darwin_id="d-1", kind="postgres", spec={})
    svc.deprovision(darwin_id="d-1", resource_id=r.id)
    with pytest.raises(ProvisioningError):
        svc.deprovision(darwin_id="d-1", resource_id=r.id)


def test_per_darwin_quota_blocks_grant():
    svc = ProvisioningService(
        quotas=ProvisioningQuotas(max_total_per_darwin=2)
    )
    svc.provision(darwin_id="d-1", kind="postgres", spec={})
    svc.provision(darwin_id="d-1", kind="bucket", spec={})
    with pytest.raises(ProvisioningError):
        svc.provision(darwin_id="d-1", kind="scheduler", spec={})


def test_per_kind_quota_blocks_grant():
    svc = ProvisioningService(
        quotas=ProvisioningQuotas(max_per_kind_per_darwin=1)
    )
    svc.provision(darwin_id="d-1", kind="postgres", spec={})
    with pytest.raises(ProvisioningError):
        svc.provision(darwin_id="d-1", kind="postgres", spec={})


def test_deprovisioning_frees_quota():
    svc = ProvisioningService(
        quotas=ProvisioningQuotas(max_per_kind_per_darwin=1)
    )
    r, _ = svc.provision(darwin_id="d-1", kind="postgres", spec={})
    svc.deprovision(darwin_id="d-1", resource_id=r.id)
    # The slot is freed, so a fresh postgres allocation succeeds.
    svc.provision(darwin_id="d-1", kind="postgres", spec={})


def test_allowed_kinds_gate():
    svc = ProvisioningService(
        quotas=ProvisioningQuotas(allowed_kinds=frozenset({"postgres"}))
    )
    svc.provision(darwin_id="d-1", kind="postgres", spec={})
    with pytest.raises(ProvisioningError):
        svc.provision(darwin_id="d-1", kind="bucket", spec={})


def test_fleet_summary_counts_active_only():
    svc = ProvisioningService()
    r1, _ = svc.provision(darwin_id="d-1", kind="postgres", spec={})
    svc.provision(darwin_id="d-1", kind="postgres", spec={})
    svc.provision(darwin_id="d-2", kind="bucket", spec={})
    svc.deprovision(darwin_id="d-1", resource_id=r1.id)
    assert svc.fleet_summary() == {"postgres": 1, "bucket": 1}


# ---------------------------------------------------------------------------
# 2. Backend isolation
# ---------------------------------------------------------------------------

class _RecordingBackend:
    """Records every provision/deprovision call so we can assert that
    quota refusals NEVER reach the backend (otherwise a misconfigured
    quota could still cost money at the cloud provider layer)."""

    def __init__(self):
        self.provision_calls = []
        self.deprovision_calls = []
        self._n = 0

    def provision(self, *, kind, spec):
        self.provision_calls.append((kind, dict(spec)))
        self._n += 1
        return {"id": f"{kind}-{self._n}", "creds": f"c-{self._n}"}

    def deprovision(self, *, kind, resource_id):
        self.deprovision_calls.append((kind, resource_id))


def test_quota_refusal_does_not_reach_backend():
    backend = _RecordingBackend()
    svc = ProvisioningService(
        backend=backend,
        quotas=ProvisioningQuotas(max_total_per_darwin=1),
    )
    svc.provision(darwin_id="d-1", kind="postgres", spec={})
    assert len(backend.provision_calls) == 1
    with pytest.raises(ProvisioningError):
        svc.provision(darwin_id="d-1", kind="bucket", spec={})
    # Backend not touched after refusal.
    assert len(backend.provision_calls) == 1


def test_backend_failure_surfaces_as_provisioning_error():
    class _Broken:
        def provision(self, *, kind, spec):
            raise RuntimeError("cloud is down")

        def deprovision(self, *, kind, resource_id):
            pass

    svc = ProvisioningService(backend=_Broken())
    with pytest.raises(ProvisioningError, match="cloud is down"):
        svc.provision(darwin_id="d-1", kind="postgres", spec={})


# ---------------------------------------------------------------------------
# 3. ScopedProvisionPort: the neuron-facing view
# ---------------------------------------------------------------------------

def test_scoped_port_matches_protocol_shape():
    svc = ProvisioningService()
    port = svc.scoped_for("d-1")
    payload = port.provision("postgres", {"size": "small"})
    assert set(payload.keys()) == {"id", "creds"}
    # Verifies the record was attributed to the scoped darwin_id, not
    # whoever called the port externally.
    rows = svc.list_resources("d-1")
    assert len(rows) == 1
    assert rows[0].kind == "postgres"


def test_scoped_port_cannot_address_other_darwins():
    """A scoped port hard-binds its darwin_id; there is no method to
    target another. This is the isolation guarantee for the genome."""
    svc = ProvisioningService()
    port_a = svc.scoped_for("d-a")
    port_a.provision("postgres", {})
    # No way for port_a to target d-b: it only exposes provision(kind, spec).
    # Demonstrate by inspecting the recorded darwin_id.
    rows = svc.list_resources("d-b")
    assert rows == []


# ---------------------------------------------------------------------------
# 4. Supervisor wiring: the genome's neuron talks to the platform service
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_supervisor_wires_scoped_port():
    """After spawn, the genome's infrastructure.provisioner neuron should
    have its ProvisionPort rebound to the supervisor's scoped view."""
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    h = sup._darwins[status.darwin_id]
    # The neuron's _provision is the scoped port from the platform service.
    neuron = h.runtime.catalog.get("infrastructure.provisioner")
    assert isinstance(neuron._provision, ScopedProvisionPort)
    # Direct call through the neuron's port lands in the supervisor's
    # service, attributed to the correct darwin_id.
    neuron._provision.provision("postgres", {"size": "small"})
    rows = sup.list_resources(status.darwin_id)
    assert len(rows) == 1
    assert rows[0].kind == "postgres"
    assert rows[0].darwin_id == status.darwin_id
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_deprovision_records_audit():
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    h = sup._darwins[status.darwin_id]
    h.runtime.catalog.get("infrastructure.provisioner")._provision.provision(
        "postgres", {}
    )
    rows = sup.list_resources(status.darwin_id)
    rid = rows[0].id
    record = await sup.deprovision_resource(
        status.darwin_id, rid, actor="alice@x.com"
    )
    assert record.status == "deprovisioned"
    # Operator audit chain has the deprovision entry tagged with the actor.
    entries = sup._audit.entries(action="deprovision_resource")
    assert len(entries) == 1
    assert entries[0].actor == "alice@x.com"
    assert entries[0].metadata["resource_id"] == rid
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_unknown_darwin_raises():
    from darwin_platform.runtime.supervisor import Supervisor, UnknownDarwin

    sup = Supervisor()
    with pytest.raises(UnknownDarwin):
        sup.list_resources("not-a-real-id")
    await sup.shutdown(kill_running=True)


# ---------------------------------------------------------------------------
# 5. HTTP surface
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http_list_and_deprovision():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    did = status.darwin_id
    # Provision two via the genome's port.
    n = sup._darwins[did].runtime.catalog.get("infrastructure.provisioner")
    n._provision.provision("postgres", {"size": "small"})
    n._provision.provision("bucket", {})

    app = build_app(sup)
    with TestClient(app) as client:
        r = client.get(f"/darwins/{did}/resources")
        assert r.status_code == 200
        rows = r.json()["resources"]
        assert {row["kind"] for row in rows} == {"postgres", "bucket"}

        # Deprovision the postgres
        pg = [row for row in rows if row["kind"] == "postgres"][0]
        r2 = client.delete(f"/darwins/{did}/resources/{pg['id']}")
        assert r2.status_code == 200
        assert r2.json()["status"] == "deprovisioned"

        # Default list filters deprovisioned
        r3 = client.get(f"/darwins/{did}/resources")
        assert {row["kind"] for row in r3.json()["resources"]} == {"bucket"}

        # include_deprovisioned=true returns both
        r4 = client.get(
            f"/darwins/{did}/resources?include_deprovisioned=true"
        )
        assert len(r4.json()["resources"]) == 2

        # Operator fleet view
        r5 = client.get("/platform/resources")
        assert r5.status_code == 200
        assert r5.json()["by_kind"] == {"bucket": 1}

    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_http_404_on_unknown_darwin():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor()
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.get("/darwins/does-not-exist/resources")
        assert r.status_code == 404
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_http_409_on_double_deprovision():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    did = status.darwin_id
    n = sup._darwins[did].runtime.catalog.get("infrastructure.provisioner")
    payload = n._provision.provision("postgres", {})
    rid = payload["id"]

    app = build_app(sup)
    with TestClient(app) as client:
        r1 = client.delete(f"/darwins/{did}/resources/{rid}")
        assert r1.status_code == 200
        r2 = client.delete(f"/darwins/{did}/resources/{rid}")
        assert r2.status_code == 409
    await sup.shutdown(kill_running=True)
