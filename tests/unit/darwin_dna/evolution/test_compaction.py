"""Generation soft-cap layer compaction (doc 06 §"soft cap at generation 20",
doc 02).

Closes gap A. Past the soft cap, the reproduction event folds the oldest
lesson layers into one consolidated layer so a long lineage's DNA does not
grow without bound, every generation still boots through the unchanged trust
root, and *legibility is preserved* — each lesson keeps the born_generation
that points at which ancestor learned it.
"""

from __future__ import annotations

import asyncio

import pytest

from darwin_dna.evolution import boot
from darwin_dna.evolution.reproduction import _compact
from darwin_dna.hyperparameters import Hyperparameters
from darwin_dna.layers import Lesson, deserialize_lessons, serialize_lessons
from darwin_dna.manifest import ManifestError, verify_manifest
from darwin_dna.signature import load_platform_public_key
from darwin_fakes import make_child_platform, make_platform


def _drive_lineage(vc, *, hp, generations):
    """Boot, reproduce, boot child, repeat; yield each generation's runtime
    and platform stack at the point of (just-booted, just-run-a-bit)."""
    platform, stack = make_platform(opening_usdc=5000.0, clock=vc)
    cur = boot(platform, hp=hp, clock=vc, sleep=vc.sleep)
    cur_stack = stack
    for _ in range(generations):
        asyncio.run(cur.run(max_heartbeats=8))
        yield cur, cur_stack
        # Force a reproduction with enough surplus to spawn.
        cur.state.balance_usdc = 4000.0
        cur_stack.wallet._balance = 4000.0
        cur.state.profit_baseline_usdc = 50.0
        cur.state.reproduce = True
        cur._reproduce()
        rec = cur_stack.spawner.children[-1]
        cplat, cstack = make_child_platform(
            signer=cur_stack.signer,
            child_manifest_json=rec["child_manifest"],
            child_layer_blobs=rec["child_layer_blobs"],
            opening_usdc=rec["budget_usdc"],
            clock=vc,
        )
        cur = boot(cplat, hp=hp, clock=vc, sleep=vc.sleep)
        cur_stack = cstack


def test_uncompacted_lineage_below_soft_cap_unchanged(vclock):
    hp = Hyperparameters(generation_soft_cap=5)
    seen = []
    for cur, _ in _drive_lineage(vclock, hp=hp, generations=4):
        gen = cur.identity.generation
        seen.append((gen, len(cur._stack._by_layer)))
        cur.audit.verify()
    # Below the cap, the founding "generation k carries k+1 layers" still holds.
    assert seen == [(0, 1), (1, 2), (2, 3), (3, 4)]


def test_past_the_soft_cap_layer_count_self_stabilises(vclock):
    hp = Hyperparameters(generation_soft_cap=3)
    cap_plus_one = hp.generation_soft_cap + 1  # the steady-state depth

    seen = []
    last_stack = None
    for cur, stack in _drive_lineage(vclock, hp=hp, generations=8):
        gen = cur.identity.generation
        layers = len(cur._stack._by_layer)
        cur.audit.verify()
        seen.append((gen, layers))
        last_stack = stack

    # The first soft_cap+1 generations are uncompacted; from then on the
    # carried depth is capped at soft_cap+1 forever — the lineage's DNA does
    # NOT grow without bound.
    for gen, layers in seen:
        expected = min(gen + 1, cap_plus_one)
        assert layers == expected, (gen, layers, expected)

    # And the lineage is still legible: even at gen 7, lessons born in every
    # ancestral generation 1..7 are still reachable through the compacted head.
    final_cur = list(_drive_lineage(vclock, hp=hp, generations=8))[-1][0]
    born = sorted({l.born_generation for l in final_cur._stack.all_lessons()})
    assert born == list(range(1, 8))

    # The reproduction event records that compaction fired.
    kinds = [r["kind"] for r in last_stack.audit_sink.read_all()]
    assert "event:reproduction.layer_compaction" in kinds


def test_compacted_child_verifies_through_the_unchanged_trust_root(vclock):
    hp = Hyperparameters(generation_soft_cap=2)
    final = list(_drive_lineage(vclock, hp=hp, generations=5))[-1][0]
    # Re-run the full trust root the way boot does, against the same key.
    pub = load_platform_public_key(
        final._p.signer.public_key_pem()
    )
    # The child carries strictly fewer layers than generation+1 (compacted)
    # and verify_manifest still accepts it.
    assert len(final._p.layer_blobs) < final.identity.generation + 1
    verify_manifest(final._p.manifest, final._p.layer_blobs, pub)


def test_verify_manifest_rejects_more_layers_than_generation_plus_one(vclock):
    # Mirror-image of the relaxation: carrying MORE layers than reproductions
    # is structurally impossible and must still be rejected.
    hp = Hyperparameters(generation_soft_cap=20)
    platform, stack = make_platform(opening_usdc=200.0, clock=vclock)
    boot(platform, hp=hp, clock=vclock, sleep=vclock.sleep)
    # Forge a manifest claiming generation 0 but carrying 2 layers.
    from darwin_dna.manifest import (
        LayerEntry, Manifest, ManifestIdentity, layer_signed_payload, sha256_hex
    )
    from darwin_dna._canonical import canonical_bytes

    signer = stack.signer
    blobs = {0: serialize_lessons([]), 1: serialize_lessons([])}
    entries = []
    for i, blob in blobs.items():
        digest = sha256_hex(blob)
        sig = signer.sign(layer_signed_payload(
            darwin_id="d", l0_version="1.0.0",
            index=i, name=f"l{i}", sha256=digest,
        )).hex()
        entries.append(LayerEntry(index=i, name=f"l{i}", sha256=digest,
                                  signature=sig))
    body = {
        "identity": ManifestIdentity(
            darwin_id="d", generation=0, parent_id=None, birth_ts=1.0,
            lineage=[], l0_version="1.0.0",
        ).model_dump(),
        "layers": [e.model_dump() for e in entries],
        "created_at": 1.0,
    }
    m = Manifest(**body, signature=signer.sign(canonical_bytes(body)).hex())
    with pytest.raises(ManifestError, match="more"):
        verify_manifest(m, blobs, load_platform_public_key(signer.public_key_pem()))


def test_compact_is_a_deterministic_id_dedupe():
    # Two layers carry the "same lesson" with the same id; the newer
    # statement wins. born_generation is preserved on the kept copy.
    old = Lesson(id="a", kind="prefer", scope="s", statement="old",
                 rationale="r", born_generation=1)
    new = Lesson(id="a", kind="prefer", scope="s", statement="new",
                 rationale="r", born_generation=4)
    other = Lesson(id="b", kind="avoid", scope="t", statement="b",
                   rationale="r", born_generation=2)
    ordered = [
        ("l0", serialize_lessons([old, other])),
        ("l1", serialize_lessons([new])),
        ("l2", serialize_lessons([])),  # an untouched recent layer
    ]
    out = _compact(ordered, soft_cap=1)  # target = 2 layers
    assert [n for n, _ in out] == ["layer_0_compacted", "l2"]
    folded = deserialize_lessons(out[0][1])
    by_id = {l.id: l for l in folded}
    assert by_id["a"].statement == "new"  # newer wins
    assert by_id["a"].born_generation == 4
    assert by_id["b"].born_generation == 2
