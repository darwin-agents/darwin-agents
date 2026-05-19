"""Paranoid trust-root tests (docs/engineering/18 hot path #1).

The properties every boot must guarantee. In each test the "attacker" is a
soma/user that has full control of the on-disk DNA bytes and only the
platform's public key is trusted.
"""

from __future__ import annotations

import pytest

from darwin_dna._canonical import canonical_bytes
from darwin_dna.layers import LayerStack, Lesson, serialize_lessons
from darwin_dna.manifest import (
    LayerEntry,
    Manifest,
    ManifestIdentity,
    ManifestError,
    layer_signed_payload,
    sha256_hex,
    verify_manifest,
)
from darwin_dna.signature import SignatureError, load_platform_public_key
from darwin_fakes.fake_platform import SigningService


def _seal(signer, *, darwin_id, l0_version, generation, layers):
    """layers: list[(name, blob)] -> a fully signed Manifest + blob map."""
    entries, blobs = [], {}
    for i, (name, blob) in enumerate(layers):
        digest = sha256_hex(blob)
        sig = signer.sign(
            layer_signed_payload(
                darwin_id=darwin_id, l0_version=l0_version,
                index=i, name=name, sha256=digest,
            )
        ).hex()
        entries.append(LayerEntry(index=i, name=name, sha256=digest, signature=sig))
        blobs[i] = blob
    body = {
        "identity": ManifestIdentity(
            darwin_id=darwin_id, generation=generation, parent_id=None,
            birth_ts=1.0, lineage=[], l0_version=l0_version,
        ).model_dump(),
        "layers": [e.model_dump() for e in entries],
        "created_at": 1.0,
    }
    manifest = Manifest(**body, signature=signer.sign(canonical_bytes(body)).hex())
    return manifest, blobs


def _lessons_blob(*ids):
    return serialize_lessons(
        [
            Lesson(id=i, kind="prefer", scope="s", statement="x",
                   rationale="y", born_generation=0)
            for i in ids
        ]
    )


@pytest.fixture
def two_layer(signer):
    m, blobs = _seal(
        signer, darwin_id="d-1", l0_version="1.0.0", generation=1,
        layers=[("layer_0_lessons", _lessons_blob()),
                ("layer_1_lessons", _lessons_blob("g1-a"))],
    )
    return m, blobs, signer


def _pub(signer):
    return load_platform_public_key(signer.public_key_pem())


def test_valid_chain_verifies(two_layer):
    m, blobs, signer = two_layer
    assert verify_manifest(m, blobs, _pub(signer)) is m
    stack = LayerStack.load(m, blobs, _pub(signer))
    assert stack.generation == 1
    assert [l.id for l in stack.all_lessons()] == ["g1-a"]


def test_tampered_layer_fails(two_layer):
    m, blobs, signer = two_layer
    blobs[1] = _lessons_blob("g1-EVIL")  # same slot, different bytes
    with pytest.raises(ManifestError, match="hash mismatch"):
        verify_manifest(m, blobs, _pub(signer))


def test_out_of_order_layers_fail(signer):
    m, blobs = _seal(
        signer, darwin_id="d", l0_version="1.0.0", generation=1,
        layers=[("a", _lessons_blob()), ("b", _lessons_blob("x"))],
    )
    swapped = Manifest(
        identity=m.identity,
        layers=[m.layers[1], m.layers[0]],  # reordered
        created_at=m.created_at,
        signature=m.signature,
    )
    with pytest.raises(ManifestError):
        verify_manifest(swapped, blobs, _pub(signer))


def test_missing_layer_fails(two_layer):
    m, blobs, signer = two_layer
    del blobs[1]
    with pytest.raises(ManifestError, match="missing or extra"):
        verify_manifest(m, blobs, _pub(signer))


def test_extra_layer_fails(two_layer):
    m, blobs, signer = two_layer
    blobs[2] = _lessons_blob("smuggled")
    with pytest.raises(ManifestError, match="missing or extra"):
        verify_manifest(m, blobs, _pub(signer))


def test_replay_layer_from_other_lineage_fails(signer):
    # A genuinely, correctly-signed layer — but minted for darwin "victim".
    other, other_blobs = _seal(
        signer, darwin_id="attacker", l0_version="1.0.0", generation=0,
        layers=[("layer_0_lessons", _lessons_blob())],
    )
    victim, victim_blobs = _seal(
        signer, darwin_id="victim", l0_version="1.0.0", generation=0,
        layers=[("layer_0_lessons", _lessons_blob())],
    )
    # Splice the attacker's real layer entry into the victim's manifest.
    spliced = Manifest(
        identity=victim.identity,
        layers=[other.layers[0]],
        created_at=victim.created_at,
        signature=victim.signature,
    )
    with pytest.raises(ManifestError):
        verify_manifest(spliced, victim_blobs, _pub(signer))


def test_replay_old_l0_version_fails(signer):
    m, blobs = _seal(
        signer, darwin_id="d", l0_version="0.9.0", generation=0,
        layers=[("layer_0_lessons", _lessons_blob())],
    )
    # Same bytes, but claim a different (current) L0 version in the manifest.
    forged = Manifest(
        identity=ManifestIdentity(
            darwin_id="d", generation=0, parent_id=None, birth_ts=1.0,
            lineage=[], l0_version="1.0.0",
        ),
        layers=m.layers,
        created_at=1.0,
        signature=m.signature,
    )
    with pytest.raises(ManifestError):
        verify_manifest(forged, blobs, _pub(signer))


def test_manifest_signed_by_wrong_key_fails(two_layer):
    m, blobs, _ = two_layer
    attacker = SigningService()
    with pytest.raises(ManifestError, match="signature"):
        verify_manifest(m, blobs, _pub(attacker))


def test_non_ed25519_key_rejected():
    with pytest.raises(SignatureError):
        load_platform_public_key(b"-----BEGIN PUBLIC KEY-----\nnope\n-----END PUBLIC KEY-----")


def test_empty_manifest_refused(signer):
    body = {
        "identity": ManifestIdentity(
            darwin_id="d", generation=0, parent_id=None, birth_ts=1.0,
            lineage=[], l0_version="1.0.0",
        ).model_dump(),
        "layers": [],
        "created_at": 1.0,
    }
    m = Manifest(**body, signature=signer.sign(canonical_bytes(body)).hex())
    with pytest.raises(ManifestError):
        verify_manifest(m, {}, _pub(signer))
