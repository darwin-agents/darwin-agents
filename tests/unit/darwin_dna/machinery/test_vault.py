"""Tiered vault: public / scoped / sensitive (doc 16 "Tiered access", 07).

Closes gap #6: the genome now enforces the three-tier read model on top of
the platform's per-Darwin namespace. Public is readable by any neuron; scoped
only by a neuron holding the matching capability tag; sensitive only with a
fresh, single-use owner grant that self-destructs after one read.
"""

from __future__ import annotations

import asyncio

import pytest

from darwin_dna.machinery.vault import (
    ScopedVaultView,
    TieredVault,
    VaultAccessError,
    VaultTier,
)
from darwin_dna.neurons import Task
from darwin_fakes.fake_vault import FakeVault, MemoryOwnerGrantStore


def _view(tags, *, raw=None, grants=None, scope=None):
    raw = raw or FakeVault()
    tv = TieredVault(raw, darwin_id="darwin-1", grants=grants)
    return ScopedVaultView(
        tv,
        capability_tags=frozenset(tags),
        default_scope=(scope or (sorted(tags)[0] if tags else "shared")),
    ), raw


# -- the tier model ----------------------------------------------------------

def test_public_is_readable_by_any_neuron():
    raw = FakeVault()
    writer, _ = _view({"content.write"}, raw=raw)
    other, _ = _view({"infra.provision"}, raw=raw)
    writer.put("display_name", "Acme", tier=VaultTier.PUBLIC)
    assert other.get("display_name", tier=VaultTier.PUBLIC) == "Acme"


def test_scoped_is_readable_only_with_the_matching_capability():
    raw = FakeVault()
    writer, _ = _view({"content.write"}, raw=raw, scope="content.write")
    writer.put("wp_creds", "secret", tier=VaultTier.SCOPED)
    assert writer.get("wp_creds", tier=VaultTier.SCOPED) == "secret"

    intruder, _ = _view({"infra.provision"}, raw=raw)
    with pytest.raises(VaultAccessError, match="not in this neuron"):
        intruder.get("wp_creds", tier=VaultTier.SCOPED, scope="content.write")


def test_sensitive_needs_a_fresh_single_use_owner_grant():
    grants = MemoryOwnerGrantStore()
    raw = FakeVault()
    v, _ = _view({"kyc.read"}, raw=raw, grants=grants)
    v.put("passport", "PII", tier=VaultTier.SENSITIVE)

    with pytest.raises(VaultAccessError, match="fresh owner grant"):
        v.get("passport", tier=VaultTier.SENSITIVE)

    grants.grant("tok-1")
    assert v.get("passport", tier=VaultTier.SENSITIVE,
                 grant_token="tok-1") == "PII"
    # Self-destructs after exactly one read.
    with pytest.raises(VaultAccessError, match="invalid or already spent"):
        v.get("passport", tier=VaultTier.SENSITIVE, grant_token="tok-1")


def test_sensitive_fails_closed_without_a_grant_store():
    v, _ = _view({"kyc.read"})  # no grants port wired
    v.put("passport", "PII", tier=VaultTier.SENSITIVE)
    with pytest.raises(VaultAccessError):
        v.get("passport", tier=VaultTier.SENSITIVE, grant_token="anything")


# -- path layout & isolation hygiene -----------------------------------------

def test_paths_mirror_the_doc16_layout():
    v, raw = _view({"content.write"}, scope="content.write")
    v.put("a", "1", tier=VaultTier.PUBLIC)
    v.put("b", "2", tier=VaultTier.SCOPED)
    v.put("c", "3", tier=VaultTier.SENSITIVE)
    assert set(raw._kv) == {
        "darwins/darwin-1/public/a",
        "darwins/darwin-1/scoped/content.write/b",
        "darwins/darwin-1/sensitive/c",
    }


def test_a_key_cannot_forge_its_own_tier_or_scope():
    v, raw = _view({"content.write"}, scope="content.write")
    v.put("../../sensitive/passport", "x", tier=VaultTier.PUBLIC)
    # The '/' is neutralised — the write stays inside the public tier.
    assert all(k.startswith("darwins/darwin-1/public/") for k in raw._kv)


def test_default_scope_round_trips_and_bare_put_still_works():
    v, _ = _view({"content.write"}, scope="content.write")
    v.put("k", "v")  # backward-compatible: defaults to scoped @ own scope
    assert v.get("k") == "v"


# -- the vault_put bridge ----------------------------------------------------

def test_vault_put_honours_tier_and_degrades_for_a_bare_backend():
    from darwin_dna.neurons._base import vault_put

    v, raw = _view({"content.write"}, scope="content.write")
    vault_put(v, "tok", "s", tier="public")
    assert "darwins/darwin-1/public/tok" in raw._kv

    class BareBackend:
        def __init__(self):
            self.kv = {}

        def put(self, key, value):  # no tier kwarg
            self.kv[key] = value

    bare = BareBackend()
    vault_put(bare, "k", "v", tier="scoped")
    assert bare.kv == {"k": "v"}


# -- wired through a real life ----------------------------------------------

def test_each_neuron_gets_a_view_scoped_to_its_own_capabilities(booted):
    rt, _ = booted
    nctx = rt._build_neuron_context("vault.writer")
    assert isinstance(nctx.vault, ScopedVaultView)
    assert nctx.vault._tags == rt._ctx.catalog["vault.writer"].capability_tags
    # The view's authority is the neuron's own tags, never the whole vault.
    assert nctx.vault._default_scope in nctx.vault._tags


def test_centralized_writer_persists_with_an_explicit_tier(booted):
    rt, stack = booted
    vw = rt._ctx.catalog["vault.writer"]
    nctx = rt._build_neuron_context("vault.writer")
    task = Task(
        id="store-1", kind="vault.write",
        required_tags=frozenset({"vault.write"}),
        payload={"key": "display_name", "value": "Acme Co", "tier": "public"},
    )
    plan = asyncio.run(vw.plan(task, nctx))
    res = asyncio.run(vw.execute(plan, nctx))
    assert res.ok
    did = rt.identity.darwin_id
    assert stack.vault.get(f"darwins/{did}/public/display_name") == "Acme Co"
    # P7 / doc 16: the key and tier are logged, the secret value never is.
    rows = repr(stack.audit_sink.read_all())
    assert "display_name" in rows and "Acme Co" not in rows
