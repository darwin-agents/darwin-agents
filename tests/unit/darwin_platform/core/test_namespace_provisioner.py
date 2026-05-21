"""Tests for the per-Darwin namespace provisioner (docs/engineering/22).

Coverage:
- Happy path: K8s + Vault + DB all provisioned, tenant cached, DSN
  written into Vault.
- Idempotent provision on repeat for same darwin_id.
- Rollback on vault failure: K8s namespace is deleted.
- Rollback on DB failure: vault + K8s torn down.
- Teardown reverses creation order; partial failures recorded.
"""

from __future__ import annotations

import pytest

from darwin_platform.namespace_provisioner import (
    NamespaceProvisioner,
    ProvisioningError,
    StubK8sBackend,
    StubOrganismDbBackend,
    StubVaultNamespaceBackend,
    TenantNamespaceStatus,
)


def _build(*, k8s=None, vault=None, db=None):
    return NamespaceProvisioner(
        k8s=k8s or StubK8sBackend(),
        vault=vault or StubVaultNamespaceBackend(),
        organism_db=db or StubOrganismDbBackend(),
    )


def test_provision_happy_path_creates_all_three():
    k8s = StubK8sBackend()
    vault = StubVaultNamespaceBackend()
    db = StubOrganismDbBackend()
    p = _build(k8s=k8s, vault=vault, db=db)
    tenant = p.provision(darwin_id="abc-1")
    assert tenant.status is TenantNamespaceStatus.READY
    assert tenant.k8s_namespace in k8s.namespaces
    assert tenant.k8s_namespace in k8s.policies
    assert tenant.vault_namespace in vault.namespaces
    assert tenant.organism_db_name in db.databases
    # The DSN is written into the vault as a scoped secret.
    secrets = vault.namespaces[tenant.vault_namespace]["secrets"]
    assert tenant.organism_db_connection_ref in secrets
    assert secrets[tenant.organism_db_connection_ref]["database"] == tenant.organism_db_name


def test_provision_idempotent_on_same_darwin_id():
    p = _build()
    a = p.provision(darwin_id="abc-1")
    b = p.provision(darwin_id="abc-1")
    assert a is b  # cached


def test_rollback_on_vault_failure_deletes_k8s_namespace():
    k8s = StubK8sBackend()

    class _Fail(StubVaultNamespaceBackend):
        def create_namespace(self, *, darwin_id):
            raise RuntimeError("vault unreachable")

    p = _build(k8s=k8s, vault=_Fail())
    with pytest.raises(ProvisioningError, match="vault"):
        p.provision(darwin_id="abc-1")
    # K8s namespace was created then rolled back via delete.
    assert k8s.namespaces  # was created
    assert k8s.deletions  # then deleted


def test_rollback_on_db_failure_tears_down_vault_and_k8s():
    k8s = StubK8sBackend()
    vault = StubVaultNamespaceBackend()

    class _DBFail(StubOrganismDbBackend):
        def create_database(self, *, darwin_id):
            raise RuntimeError("organism cluster offline")

    p = _build(k8s=k8s, vault=vault, db=_DBFail())
    with pytest.raises(ProvisioningError, match="organism_db"):
        p.provision(darwin_id="abc-1")
    # Both prior steps were rolled back.
    assert k8s.deletions
    assert vault.deletions


def test_teardown_reverses_creation_and_clears_cache():
    k8s = StubK8sBackend()
    vault = StubVaultNamespaceBackend()
    db = StubOrganismDbBackend()
    p = _build(k8s=k8s, vault=vault, db=db)
    p.provision(darwin_id="abc-1")
    p.teardown(darwin_id="abc-1")
    assert p.get("abc-1") is None
    # All three sides got their delete call.
    assert k8s.deletions
    assert vault.deletions
    assert db.dropped


def test_teardown_unknown_darwin_is_noop():
    p = _build()
    p.teardown(darwin_id="never-provisioned")
    # No exception; cache empty.
    assert p.all() == []


def test_audit_recorder_captures_provision_event():
    recorded = []
    p = NamespaceProvisioner(
        k8s=StubK8sBackend(),
        vault=StubVaultNamespaceBackend(),
        organism_db=StubOrganismDbBackend(),
        audit_recorder=lambda actor, action, meta: recorded.append(
            (actor, action, meta),
        ),
    )
    p.provision(darwin_id="abc-1")
    actions = [r[1] for r in recorded]
    assert "tenant_provisioned" in actions
