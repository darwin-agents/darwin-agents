"""Real vault backend adapter (docs/engineering/16).

Exercises the adapter against a stub :class:`VaultClient`. The genome's
:class:`TieredVault` already composes the per-Darwin path prefix; the
backend just stores keys verbatim.
"""

from __future__ import annotations

import pytest

from darwin_platform.adapters import (
    InMemoryVaultClient,
    RealVaultBackend,
    VaultClient,
    VaultError,
)


# -- construction --------------------------------------------------------
def test_requires_vault_client():
    with pytest.raises(TypeError, match="VaultClient"):
        RealVaultBackend("not a client")  # type: ignore[arg-type]


def test_in_memory_client_is_a_VaultClient():
    """The default in-memory stub conforms to the runtime-checkable
    Protocol — i.e. the production hvac wrapper just needs the same
    three methods."""
    assert isinstance(InMemoryVaultClient(), VaultClient)


# -- put / get / delete round-trip ---------------------------------------
def test_put_get_round_trip():
    b = RealVaultBackend(InMemoryVaultClient())
    b.put("darwins/x/public/display_name", "Lana")
    assert b.get("darwins/x/public/display_name") == "Lana"


def test_get_missing_returns_none():
    b = RealVaultBackend(InMemoryVaultClient())
    assert b.get("nope") is None


def test_delete_removes_key():
    b = RealVaultBackend(InMemoryVaultClient())
    b.put("k", "v")
    b.delete("k")
    assert b.get("k") is None


def test_delete_missing_key_is_silent():
    b = RealVaultBackend(InMemoryVaultClient())
    b.delete("nope")  # no exception


# -- error handling ------------------------------------------------------
class _RaisingClient:
    def kv_put(self, key, value):
        raise RuntimeError("network down")

    def kv_get(self, key):
        raise RuntimeError("read failed")

    def kv_delete(self, key):
        raise RuntimeError("delete failed")


def test_put_surfaces_backend_failure_as_vault_error():
    b = RealVaultBackend(_RaisingClient())
    with pytest.raises(VaultError, match="put"):
        b.put("k", "v")


def test_get_fails_closed_on_backend_failure():
    """Sensitive-tier reads must FAIL CLOSED — the genome's tiered vault
    interprets None as 'no such key' and refuses to surface anything.
    The adapter therefore must NOT propagate backend exceptions on
    get(); a returned None is the safe choice."""
    b = RealVaultBackend(_RaisingClient())
    assert b.get("k") is None


def test_delete_surfaces_backend_failure():
    b = RealVaultBackend(_RaisingClient())
    with pytest.raises(VaultError, match="delete"):
        b.delete("k")


# -- swap-in with the genome's TieredVault -------------------------------
def test_real_backend_is_drop_in_for_FakeVault() -> None:
    """The genome's TieredVault uses ``put(key, value) / get(key)`` on
    the backing store. Confirm the real adapter implements that
    surface so swapping it in works without touching the genome."""
    from darwin_dna.machinery.vault import TieredVault, VaultTier
    from darwin_fakes import MemoryOwnerGrantStore

    backend = RealVaultBackend(InMemoryVaultClient())
    vault = TieredVault(
        backend,
        darwin_id="darwin-abc",
        grants=MemoryOwnerGrantStore(),
    )
    # Public tier is unrestricted; round-trip a value.
    vault.put(VaultTier.PUBLIC, "hello", "world")
    assert vault.read(
        VaultTier.PUBLIC, "hello", neuron_tags=frozenset()
    ) == "world"


def test_keys_are_passed_through_verbatim() -> None:
    """The genome's TieredVault composes 'darwins/{id}/{tier}/...' paths.
    The adapter must store those keys verbatim — no munging — so a real
    Vault deployment's per-tenant namespace mapping (in the
    VaultClient impl) sees the same paths the genome writes."""
    client = InMemoryVaultClient()
    b = RealVaultBackend(client)
    composed = "darwins/d1/sensitive/kyc/passport"
    b.put(composed, "redacted-bytes")
    assert client._kv[composed] == "redacted-bytes"  # noqa: SLF001
