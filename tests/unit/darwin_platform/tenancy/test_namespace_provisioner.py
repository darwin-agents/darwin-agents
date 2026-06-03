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

from darwin_platform.tenancy.namespace_provisioner import (
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


def test_pod_bootstrap_carries_approle_and_dsn_consumed_once():
    p = _build()
    p.provision(darwin_id="abc-1")
    boot = p.consume_pod_bootstrap("abc-1")
    assert boot is not None
    assert boot["vault_role_id"] and boot["vault_secret_id"]
    assert boot["organism_db_dsn"]
    # One-shot: a second consume yields nothing (secret material isn't retained).
    assert p.consume_pod_bootstrap("abc-1") is None


def test_secret_id_never_recorded_on_tenant_namespace():
    p = _build()
    tenant = p.provision(darwin_id="abc-1")
    boot = p.consume_pod_bootstrap("abc-1")
    secret_id = boot["vault_secret_id"]
    # The secret_id must not leak through the serialisable / audited record.
    blob = str(tenant.to_json())
    assert secret_id not in blob
    assert "approle_role_id" in tenant.metadata  # role_id is safe to record
    assert "secret_id" not in tenant.metadata


def test_teardown_clears_unconsumed_bootstrap():
    p = _build()
    p.provision(darwin_id="abc-1")
    p.teardown(darwin_id="abc-1")  # never consumed
    assert p.consume_pod_bootstrap("abc-1") is None


def test_provision_surfaces_readonly_dsn_when_backend_exposes_it():
    class _RoBackend(StubOrganismDbBackend):
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

    p = _build(db=_RoBackend())
    p.provision(darwin_id="abc-1")
    ro = p.organism_readonly_dsn("abc-1")
    assert ro is not None and "_ro:" in ro
    # Cleared on teardown (it carries a credential).
    p.teardown(darwin_id="abc-1")
    assert p.organism_readonly_dsn("abc-1") is None


def test_readonly_dsn_none_when_backend_has_no_ro_role():
    p = _build()  # StubOrganismDbBackend has no readonly_credentials
    p.provision(darwin_id="abc-1")
    assert p.organism_readonly_dsn("abc-1") is None


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


# ---------------------------------------------------------------------------
# wallet keypair provisioning (step 4) — doc 17 per-Darwin self-custodial
# ---------------------------------------------------------------------------


class _StubWalletKeypair:
    def __init__(self, chain: str, secret: str, address: str) -> None:
        self.chain = chain
        self.secret = secret
        self.address = address


class _StubMinter:
    """Returns a pre-canned keypair so tests assert exact addresses."""

    def __init__(self, chain: str, secret: str, address: str) -> None:
        self.chain = chain
        self._secret = secret
        self._address = address
        self.calls = 0

    def mint(self) -> _StubWalletKeypair:
        self.calls += 1
        return _StubWalletKeypair(self.chain, self._secret, self._address)


def test_wallet_keypair_minters_seal_secrets_in_vault_namespace():
    vault = StubVaultNamespaceBackend()
    sol = _StubMinter("solana", secret="sol-sec", address="sol-addr")
    stl = _StubMinter("stellar", secret="stl-sec", address="stl-addr")
    p = NamespaceProvisioner(
        k8s=StubK8sBackend(),
        vault=vault,
        organism_db=StubOrganismDbBackend(),
        wallet_keypair_minters={"solana": sol, "stellar": stl},
    )
    tenant = p.provision(darwin_id="abc-1")
    # Both minters fired exactly once; the namespace holds both secrets.
    assert sol.calls == 1
    assert stl.calls == 1
    secrets = vault.namespaces[tenant.vault_namespace]["secrets"]
    assert secrets["kv/wallet/solana"] == {
        "secret": "sol-sec", "address": "sol-addr",
    }
    assert secrets["kv/wallet/stellar"] == {
        "secret": "stl-sec", "address": "stl-addr",
    }
    # Addresses (not secrets) are surfaced on the tenant for downstream
    # use (omnibus.fund_child, parent transfer destinations).
    assert tenant.wallet_addresses == {
        "solana": "sol-addr", "stellar": "stl-addr",
    }


def test_wallet_keypair_minters_omitted_keeps_addresses_empty():
    """Default behaviour without minters: lazy wallet-factory path keeps
    sole responsibility for keypair provisioning."""
    p = _build()
    tenant = p.provision(darwin_id="abc-1")
    assert tenant.wallet_addresses == {}


def test_wallet_keypair_step_audit_includes_addresses_not_secrets():
    """The audit record should expose the on-chain addresses (safe) but
    must not include the secrets (which would leak to anywhere the audit
    sink ships to)."""
    recorded = []
    sol = _StubMinter("solana", secret="MUST-NOT-LEAK", address="sol-addr")
    p = NamespaceProvisioner(
        k8s=StubK8sBackend(),
        vault=StubVaultNamespaceBackend(),
        organism_db=StubOrganismDbBackend(),
        audit_recorder=lambda actor, action, meta: recorded.append(
            (actor, action, meta),
        ),
        wallet_keypair_minters={"solana": sol},
    )
    p.provision(darwin_id="abc-1")
    provisioned = [r for r in recorded if r[1] == "tenant_provisioned"]
    assert len(provisioned) == 1
    meta = provisioned[0][2]
    assert meta["wallet_addresses"] == {"solana": "sol-addr"}
    assert "MUST-NOT-LEAK" not in str(meta)


def test_wallet_keypair_step_failure_rolls_back_db_vault_k8s():
    """If the wallet step explodes, the prior three steps must roll back
    so we don't leak orphan K8s/Vault/DB resources."""
    k8s = StubK8sBackend()
    vault = StubVaultNamespaceBackend()
    db = StubOrganismDbBackend()

    class _BoomMinter:
        chain = "solana"

        def mint(self):
            raise RuntimeError("solana RPC down")

    p = NamespaceProvisioner(
        k8s=k8s, vault=vault, organism_db=db,
        wallet_keypair_minters={"solana": _BoomMinter()},
    )
    with pytest.raises(ProvisioningError, match="wallet_keypair"):
        p.provision(darwin_id="abc-1")
    # All three earlier steps were rolled back.
    assert k8s.deletions
    assert vault.deletions
    assert db.dropped


def test_wallet_keypair_minter_chain_mismatch_rejected():
    """A misconfigured factory that pairs a solana minter under the
    'base' key would silently write Solana-shaped bytes into the base
    slot — refuse outright."""
    sol = _StubMinter("solana", "x", "y")
    p = NamespaceProvisioner(
        k8s=StubK8sBackend(),
        vault=StubVaultNamespaceBackend(),
        organism_db=StubOrganismDbBackend(),
        wallet_keypair_minters={"base": sol},  # mismatch
    )
    with pytest.raises(ProvisioningError, match="declares chain"):
        p.provision(darwin_id="abc-1")
