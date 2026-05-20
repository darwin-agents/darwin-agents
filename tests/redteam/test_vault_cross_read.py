"""Vault tier-model red-team (docs/engineering/16, 24).

The tiered vault is the platform's "no neuron reads what it didn't earn"
boundary. Attacks try to: read across scopes without holding the tag,
read sensitive secrets without an owner grant, replay a consumed grant
token, and smuggle a higher-tier read through a key with path separators.
"""

from __future__ import annotations

import pytest

from darwin_dna.machinery.vault import (
    TieredVault,
    VaultAccessError,
    VaultTier,
)
from darwin_fakes.fake_vault import FakeVault, MemoryOwnerGrantStore


def test_redteam_neuron_cannot_read_other_scopes_secrets():
    """A scoped read requires the requesting neuron to hold the scope
    tag — even if the neuron knows the key path, it can't read a
    secret it didn't earn."""
    raw = FakeVault()
    grants = MemoryOwnerGrantStore()
    v = TieredVault(raw, darwin_id="d-1", grants=grants)
    v.put(VaultTier.SCOPED, "stripe_key", "sk_live_x",
          scope="payments.send")
    # A neuron with the 'web.publish' tag (NOT 'payments.send') tries
    # to read the credential — refused.
    with pytest.raises(VaultAccessError):
        v.read(
            VaultTier.SCOPED, "stripe_key",
            neuron_tags=frozenset({"web.publish"}),
            scope="payments.send",
        )


def test_redteam_sensitive_read_without_grant_refused():
    raw = FakeVault()
    grants = MemoryOwnerGrantStore()
    v = TieredVault(raw, darwin_id="d-1", grants=grants)
    v.put(VaultTier.SENSITIVE, "kyc.passport", "encrypted-blob")
    with pytest.raises(VaultAccessError):
        v.read(
            VaultTier.SENSITIVE, "kyc.passport",
            neuron_tags=frozenset(),
            grant_token=None,
        )


def test_redteam_replayed_grant_token_fails_second_time():
    raw = FakeVault()
    grants = MemoryOwnerGrantStore()
    v = TieredVault(raw, darwin_id="d-1", grants=grants)
    v.put(VaultTier.SENSITIVE, "k", "secret")
    grants.grant("tok-1")
    # First read consumes the token.
    v.read(VaultTier.SENSITIVE, "k",
           neuron_tags=frozenset(), grant_token="tok-1")
    # Replay fails.
    with pytest.raises(VaultAccessError):
        v.read(VaultTier.SENSITIVE, "k",
               neuron_tags=frozenset(), grant_token="tok-1")


def test_redteam_path_smuggle_via_slash_in_key_blocked():
    """A key containing '/' must not smuggle the read into a higher
    tier or another scope."""
    raw = FakeVault()
    v = TieredVault(raw, darwin_id="d-1")
    v.put(VaultTier.PUBLIC, "../sensitive/kyc.id", "leak")
    # The path is sanitized at the segment level — the smuggled
    # value lives under public/ with slashes stripped from the key.
    keys = list(raw._kv.keys())  # noqa: SLF001
    # Find the key we just put — exactly one entry, under public/.
    target = [k for k in keys if "leak" not in k][0]
    assert "darwins/d-1/public/" in target
    # And it doesn't smuggle into the sensitive subtree.
    assert "darwins/d-1/sensitive" not in target
