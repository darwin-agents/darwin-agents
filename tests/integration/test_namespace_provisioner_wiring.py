"""Wiring the per-Darwin NamespaceProvisioner end-to-end (doc 22; audit #19).

Two halves:

- ``_build_namespace_provisioner`` constructs the three real backends from CLI
  flags and fails loudly (exit 2) when a credential is missing — mirroring the
  other real-backend flags.
- the k8s spawn path now actually *calls* ``provision`` (it previously skipped
  it, so a k8s-mode spawn provisioned no Vault namespace / organism DB — the
  exact "built but never wired" gap the audit flagged).
"""

from __future__ import annotations

import pytest

from darwin_platform.__main__ import _build_namespace_provisioner, build_parser
from darwin_platform.runtime.driver import DarwinSnapshot, DarwinSpec
from darwin_platform.runtime.supervisor import Supervisor
from darwin_platform.tenancy.namespace_provisioner import (
    NamespaceProvisioner,
    StubK8sBackend,
    StubOrganismDbBackend,
    StubVaultNamespaceBackend,
    TenantNamespaceStatus,
)


def _args(*extra: str):
    return build_parser().parse_args(["serve", *extra])


# -- _build_namespace_provisioner -----------------------------------------
def test_returns_none_without_flag():
    assert _build_namespace_provisioner(_args(), None) is None


def test_missing_vault_creds_exits(monkeypatch):
    monkeypatch.delenv("DARWIN_VAULT_URL", raising=False)
    monkeypatch.delenv("DARWIN_VAULT_TOKEN", raising=False)
    with pytest.raises(SystemExit) as ei:
        _build_namespace_provisioner(
            _args("--tenant-namespaces",
                  "--organism-db-admin-url", "postgresql://a:b@h:5432/postgres"),
            None,
        )
    assert ei.value.code == 2


def test_missing_organism_url_exits(monkeypatch):
    monkeypatch.delenv("DARWIN_ORGANISM_DB_ADMIN_URL", raising=False)
    with pytest.raises(SystemExit) as ei:
        _build_namespace_provisioner(
            _args("--tenant-namespaces",
                  "--vault-url", "http://vault:8200",
                  "--vault-token", "t"),
            None,
        )
    assert ei.value.code == 2


def test_inprocess_builds_provisioner_with_stub_k8s():
    prov = _build_namespace_provisioner(
        _args("--tenant-namespaces",
              "--vault-url", "http://vault:8200", "--vault-token", "t",
              "--organism-db-admin-url", "postgresql://a:b@h:5432/postgres"),
        None,
    )
    assert isinstance(prov, NamespaceProvisioner)
    # In-process: no real K8s namespace, so the stub backend is used.
    assert isinstance(prov._k8s, StubK8sBackend)  # noqa: SLF001


def test_k8s_mode_uses_driver_backend():
    from darwin_platform.k8s.namespace_backend import DriverK8sNamespaceBackend

    class _FakeApi:
        def delete_namespace(self, name): ...

    class _FakeDriver:
        api = _FakeApi()
        namespace_prefix = "tenant-"

    prov = _build_namespace_provisioner(
        _args("--tenant-namespaces",
              "--vault-url", "http://vault:8200", "--vault-token", "t",
              "--organism-db-admin-url", "postgresql://a:b@h:5432/postgres"),
        _FakeDriver(),
    )
    assert isinstance(prov._k8s, DriverK8sNamespaceBackend)  # noqa: SLF001
    # Derived names use the driver's prefix.
    assert prov._k8s.create_namespace(darwin_id="d1").startswith("tenant-")  # noqa: SLF001


# -- the k8s spawn path now provisions ------------------------------------
class _FakeK8sDriver:
    def __init__(self):
        self.started: list[DarwinSpec] = []

    async def start(self, spec: DarwinSpec) -> DarwinSnapshot:
        self.started.append(spec)
        return DarwinSnapshot(
            darwin_id=spec.darwin_id, alive=True, paused=False, killed=False,
            tick=0, days_elapsed=0.0, balance_usdc=spec.opening_usdc,
        )

    async def stop(self, darwin_id: str) -> None: ...
    async def kill(self, darwin_id, *, reason): ...
    async def snapshot(self, darwin_id: str) -> DarwinSnapshot:
        return DarwinSnapshot(
            darwin_id=darwin_id, alive=True, paused=False, killed=False,
            tick=0, days_elapsed=0.0, balance_usdc=0.0,
        )


def _stub_provisioner() -> NamespaceProvisioner:
    return NamespaceProvisioner(
        k8s=StubK8sBackend(),
        vault=StubVaultNamespaceBackend(),
        organism_db=StubOrganismDbBackend(),
    )


async def test_k8s_spawn_provisions_tenant_namespace():
    driver = _FakeK8sDriver()
    prov = _stub_provisioner()
    sup = Supervisor(
        k8s_driver=driver, heartbeat_seconds=0.0, namespace_provisioner=prov,
    )
    try:
        status = await sup.spawn(opening_usdc=50.0)
        # The gap fix: the k8s path provisioned the tenant before the pod boot.
        tenant = prov.get(status.darwin_id)
        assert tenant is not None
        assert tenant.status is TenantNamespaceStatus.READY
        # Vault namespace + organism DB were actually created via the backends.
        assert prov._vault.namespaces  # noqa: SLF001
        assert prov._db.databases  # noqa: SLF001
    finally:
        await sup.shutdown()


async def test_k8s_spawn_injects_vault_approle_into_pod_secret():
    """The provisioned per-Darwin AppRole + organism DSN are sealed into the
    DarwinSpec.secret_extras the driver projects into the pod's Secret — so the
    pod authenticates to Vault as its own scoped identity (doc 22)."""
    from darwin_platform.k8s.manifests import (
        ORGANISM_DB_DSN_SECRET_KEY,
        VAULT_APPROLE_ROLE_ID_SECRET_KEY,
        VAULT_APPROLE_SECRET_ID_SECRET_KEY,
    )

    driver = _FakeK8sDriver()
    prov = _stub_provisioner()
    sup = Supervisor(
        k8s_driver=driver, heartbeat_seconds=0.0, namespace_provisioner=prov,
    )
    try:
        await sup.spawn(opening_usdc=50.0)
        spec = driver.started[0]
        extras = spec.secret_extras
        assert extras[VAULT_APPROLE_ROLE_ID_SECRET_KEY]
        assert extras[VAULT_APPROLE_SECRET_ID_SECRET_KEY]
        assert extras[ORGANISM_DB_DSN_SECRET_KEY]
    finally:
        await sup.shutdown()


async def test_organism_reader_built_from_provisioner_readonly_dsn():
    """The supervisor builds a read-only OrganismDbReader from the per-Darwin
    read-only DSN the provisioner surfaced (doc 08 data browser)."""
    from darwin_platform.adapters.organism_reader import OrganismDbReader

    class _RoOrganismBackend(StubOrganismDbBackend):
        def __init__(self):
            super().__init__()
            self.readonly_credentials = {}

        def create_database(self, *, darwin_id):
            db, role, dsn = super().create_database(darwin_id=darwin_id)
            self.readonly_credentials[db] = {
                "role": f"{role}_ro", "password": "p",
                "dsn": f"postgresql://{role}_ro:p@db/{db}",
            }
            return db, role, dsn

    prov = NamespaceProvisioner(
        k8s=StubK8sBackend(), vault=StubVaultNamespaceBackend(),
        organism_db=_RoOrganismBackend(),
    )
    sup = Supervisor(heartbeat_seconds=0.0, namespace_provisioner=prov)
    try:
        status = await sup.spawn(opening_usdc=10.0)
        did = status.darwin_id
        reader = sup.organism_reader_for(did)
        assert isinstance(reader, OrganismDbReader)
        # Built from the SELECT-only role's DSN, not the owner's.
        assert "_ro:" in reader._dsn  # noqa: SLF001
    finally:
        await sup.shutdown(kill_running=True)


async def test_organism_reader_none_without_provisioner():
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        status = await sup.spawn(opening_usdc=10.0)
        assert sup.organism_reader_for(status.darwin_id) is None
        import pytest as _pytest
        with _pytest.raises(Exception):
            sup.list_organism_tables(status.darwin_id)
    finally:
        await sup.shutdown(kill_running=True)


async def test_k8s_spawn_tears_down_tenant_when_pod_start_fails():
    class _FailingDriver(_FakeK8sDriver):
        async def start(self, spec):
            raise RuntimeError("pod scheduling failed")

    driver = _FailingDriver()
    prov = _stub_provisioner()
    sup = Supervisor(
        k8s_driver=driver, heartbeat_seconds=0.0, namespace_provisioner=prov,
    )
    try:
        with pytest.raises(RuntimeError, match="pod scheduling failed"):
            await sup.spawn(opening_usdc=50.0)
        # No leaked tenant: the just-provisioned Vault namespace + DB were torn
        # down (immediate, not the 90-day deferred path — nothing ran).
        assert prov.all() == []
        assert prov._db.dropped  # noqa: SLF001
    finally:
        await sup.shutdown()
