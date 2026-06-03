"""Real hvac-backed per-Darwin Vault isolation (doc 16 §"Namespace per Darwin").

These tests inject a recording hvac-shaped stub so the backend is exercised
end-to-end (policy HCL, AppRole binding, KV path composition, teardown)
without a real Vault. The load-bearing property is the *scoped policy*: a
Darwin's policy grants its own subtree and no other's, so Vault — not app code
— enforces tenant isolation.
"""

from __future__ import annotations

import pytest

from darwin_platform.adapters.vault_namespace import (
    RealVaultNamespaceBackend,
    VaultNamespaceError,
)
from darwin_platform.tenancy.namespace_provisioner import VaultNamespaceBackend


# -- recording hvac stub ---------------------------------------------------
class _Sys:
    def __init__(self, *, enable_error: Exception | None = None):
        self.policies: dict[str, str] = {}
        self.deleted_policies: list[str] = []
        self.enabled: list[dict] = []
        self._enable_error = enable_error

    def enable_auth_method(self, *, method_type, path):
        if self._enable_error is not None:
            raise self._enable_error
        self.enabled.append({"method_type": method_type, "path": path})

    def create_or_update_policy(self, *, name, policy):
        self.policies[name] = policy

    def delete_policy(self, *, name):
        self.deleted_policies.append(name)
        self.policies.pop(name, None)


class _AppRole:
    def __init__(self):
        self.roles: dict[str, dict] = {}
        self.deleted: list[str] = []

    def create_or_update_approle(self, *, role_name, token_policies, **_):
        self.roles[role_name] = {"token_policies": list(token_policies)}

    def read_role_id(self, *, role_name, **_):
        return {"data": {"role_id": f"role-id-{role_name}"}}

    def generate_secret_id(self, *, role_name, **_):
        return {"data": {"secret_id": f"secret-id-{role_name}"}}

    def list_roles(self, **_):
        return {"data": {"keys": list(self.roles)}}

    def delete_role(self, *, role_name, **_):
        self.deleted.append(role_name)
        self.roles.pop(role_name, None)


class _KvV2:
    def __init__(self):
        self.secrets: dict[str, dict] = {}
        self.deleted: list[str] = []

    def create_or_update_secret(self, *, mount_point, path, secret):
        self.secrets[path] = dict(secret)

    def list_secrets(self, *, mount_point, path):
        # Return immediate children of `path`. Mirrors Vault KV v2: a child
        # that has further segments is a "directory" and comes back with a
        # trailing slash; a leaf comes back bare.
        prefix = path.rstrip("/") + "/"
        keys = set()
        for p in self.secrets:
            if p.startswith(prefix):
                rest = p[len(prefix):]
                head, sep, _ = rest.partition("/")
                keys.add(head + "/" if sep else head)
        if not keys:
            raise RuntimeError("no secrets at path")
        return {"data": {"keys": sorted(keys)}}

    def delete_metadata_and_all_versions(self, *, mount_point, path):
        self.deleted.append(path)
        self.secrets.pop(path, None)


class _Auth:
    def __init__(self):
        self.approle = _AppRole()


class _Secrets:
    def __init__(self):
        self.kv = type("KV", (), {})()
        self.kv.v2 = _KvV2()


class _StubClient:
    def __init__(self, *, enable_error: Exception | None = None):
        self.sys = _Sys(enable_error=enable_error)
        self.auth = _Auth()
        self.secrets = _Secrets()


def _backend(**kw):
    return RealVaultNamespaceBackend(client=_StubClient(**kw), mount_point="secret")


# -- conformance -----------------------------------------------------------
def test_conforms_to_protocol():
    assert isinstance(_backend(), VaultNamespaceBackend)


# -- create_namespace ------------------------------------------------------
def test_create_namespace_writes_scoped_policy_and_returns_prefix():
    b = _backend()
    ns = b.create_namespace(darwin_id="abc")
    assert ns == "darwins/abc"
    policy = b._client.sys.policies["darwin-abc"]  # noqa: SLF001
    # Grants this Darwin's own subtree (data + metadata)...
    assert 'path "secret/data/darwins/abc/*"' in policy
    assert 'path "secret/metadata/darwins/abc/*"' in policy
    # ...and approle auth was enabled.
    assert b._client.sys.enabled[0]["method_type"] == "approle"  # noqa: SLF001


def test_policy_does_not_grant_another_darwins_paths():
    """The isolation property: Darwin A's policy must not mention B's paths."""
    b = _backend()
    b.create_namespace(darwin_id="aaa")
    policy = b._client.sys.policies["darwin-aaa"]  # noqa: SLF001
    assert "darwins/bbb" not in policy
    assert "darwins/aaa" in policy


def test_approle_enabled_only_once_across_calls():
    b = _backend()
    b.create_namespace(darwin_id="a1")
    b.create_namespace(darwin_id="a2")
    assert len(b._client.sys.enabled) == 1  # noqa: SLF001


def test_create_namespace_tolerates_already_enabled_approle():
    # hvac raises a 400 "path is already in use" when approle is on already.
    b = _backend(enable_error=RuntimeError("path is already in use"))
    ns = b.create_namespace(darwin_id="abc")  # must not raise
    assert ns == "darwins/abc"


def test_create_namespace_propagates_unexpected_enable_error():
    b = _backend(enable_error=RuntimeError("connection refused"))
    with pytest.raises(VaultNamespaceError):
        b.create_namespace(darwin_id="abc")


def test_create_namespace_requires_darwin_id():
    with pytest.raises(VaultNamespaceError):
        _backend().create_namespace(darwin_id="")


# -- issue_approle ---------------------------------------------------------
def test_issue_approle_binds_to_scoped_policy():
    b = _backend()
    ns = b.create_namespace(darwin_id="abc")
    creds = b.issue_approle(namespace=ns, role_name="orchestrator")
    assert creds["role_id"].startswith("role-id-")
    assert creds["secret_id"].startswith("secret-id-")
    # The role is bound to THIS Darwin's policy only.
    role = b._client.auth.approle.roles["darwins-abc-orchestrator"]  # noqa: SLF001
    assert role["token_policies"] == ["darwin-abc"]


# -- write_secret ----------------------------------------------------------
def test_write_secret_composes_scoped_path():
    b = _backend()
    ns = b.create_namespace(darwin_id="abc")
    b.write_secret(namespace=ns, path="kv/organism/dsn", value={"dsn": "x"})
    assert "darwins/abc/kv/organism/dsn" in b._client.secrets.kv.v2.secrets  # noqa: SLF001


# -- delete_namespace ------------------------------------------------------
def test_delete_namespace_revokes_policy_and_approles():
    b = _backend()
    ns = b.create_namespace(darwin_id="abc")
    b.issue_approle(namespace=ns, role_name="orchestrator")
    b.write_secret(namespace=ns, path="kv/wallet/base", value={"k": "v"})

    b.delete_namespace(namespace=ns)
    # Policy gone (capability revoked for any lingering token)...
    assert "darwin-abc" in b._client.sys.deleted_policies  # noqa: SLF001
    # ...AppRole gone...
    assert "darwins-abc-orchestrator" in b._client.auth.approle.deleted  # noqa: SLF001
    # ...and the KV subtree purged.
    assert "darwins/abc/kv/wallet/base" in b._client.secrets.kv.v2.deleted  # noqa: SLF001
