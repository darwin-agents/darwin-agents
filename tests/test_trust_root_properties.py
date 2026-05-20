"""Trust-root property/fuzz tests (doc 18 hot-path #1).

Doc 18 mandates property-based + fuzz tests on signature/manifest. The
example-based suite in ``test_signature.py`` pins specific scenarios; this
suite generalises: across many randomly-shaped sealed chains, the trust root
must catch every tamper, every reorder, every missing/extra/replayed layer,
and only accept what was actually signed by the platform key.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from darwin_dna._canonical import canonical_bytes
from darwin_dna.manifest import (
    LayerEntry,
    Manifest,
    ManifestError,
    ManifestIdentity,
    layer_signed_payload,
    sha256_hex,
    verify_manifest,
)
from darwin_dna.signature import load_platform_public_key
from darwin_fakes.fake_platform import SigningService


# Bounded strategies — fast property tests, not full random fuzzing of bignums.
_ID = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
              min_size=1, max_size=12)
_VER = st.from_regex(r"\A[0-9]\.[0-9]\.[0-9]\Z", fullmatch=True)
_NAME = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_0123456789",
                min_size=1, max_size=16)
_BLOB = st.binary(min_size=0, max_size=64)


def _seal_chain(
    signer,
    *,
    darwin_id: str,
    l0_version: str,
    generation: int,
    layers: list[tuple[str, bytes]],
) -> tuple[Manifest, dict[int, bytes]]:
    """Build a fully-signed manifest + blobs for an arbitrary ordered layer
    list. ``generation`` can be set independently of ``len(layers)`` so the
    test can exercise compaction-shaped chains."""
    entries, blobs = [], {}
    for i, (name, blob) in enumerate(layers):
        digest = sha256_hex(blob)
        sig = signer.sign(
            layer_signed_payload(
                darwin_id=darwin_id, l0_version=l0_version,
                index=i, name=name, sha256=digest,
            )
        ).hex()
        entries.append(LayerEntry(index=i, name=name, sha256=digest,
                                  signature=sig))
        blobs[i] = blob
    body = {
        "identity": ManifestIdentity(
            darwin_id=darwin_id, generation=generation, parent_id=None,
            birth_ts=1.0, lineage=[], l0_version=l0_version,
        ).model_dump(),
        "layers": [e.model_dump() for e in entries],
        "created_at": 1.0,
    }
    return Manifest(
        **body, signature=signer.sign(canonical_bytes(body)).hex()
    ), blobs


def _pub(signer):
    return load_platform_public_key(signer.public_key_pem())


# -- round trip: every honestly-sealed chain verifies ------------------------

@settings(max_examples=80, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(
    darwin_id=_ID, l0_version=_VER,
    layer_blobs=st.lists(_BLOB, min_size=1, max_size=6),
)
def test_honestly_sealed_chain_round_trips(darwin_id, l0_version, layer_blobs):
    signer = SigningService()
    chain = [(f"layer_{i}", b) for i, b in enumerate(layer_blobs)]
    m, blobs = _seal_chain(
        signer, darwin_id=darwin_id, l0_version=l0_version,
        generation=len(chain) - 1, layers=chain,
    )
    assert verify_manifest(m, blobs, _pub(signer)) is m


# -- tamper: flipping any layer's bytes is caught ----------------------------

@settings(max_examples=60, deadline=None)
@given(
    n=st.integers(min_value=1, max_value=5),
    flip_at=st.integers(min_value=0),
    replacement=_BLOB,
)
def test_any_tampered_layer_fails(n, flip_at, replacement):
    signer = SigningService()
    chain = [(f"l{i}", bytes([i + 1]) * (i + 1)) for i in range(n)]
    m, blobs = _seal_chain(
        signer, darwin_id="d", l0_version="1.0.0",
        generation=n - 1, layers=chain,
    )
    idx = flip_at % n
    if replacement == blobs[idx]:
        replacement = replacement + b"\x00"  # ensure it actually differs
    blobs[idx] = replacement
    with pytest.raises(ManifestError):
        verify_manifest(m, blobs, _pub(signer))


# -- ordering: any non-identity permutation is caught -----------------------

@settings(max_examples=40, deadline=None)
@given(st.integers(min_value=2, max_value=5))
def test_any_reordering_fails(n):
    signer = SigningService()
    chain = [(f"l{i}", bytes([i])) for i in range(n)]
    m, blobs = _seal_chain(
        signer, darwin_id="d", l0_version="1.0.0",
        generation=n - 1, layers=chain,
    )
    swapped = Manifest(
        identity=m.identity,
        layers=list(reversed(m.layers)),
        created_at=m.created_at,
        signature=m.signature,
    )
    with pytest.raises(ManifestError):
        verify_manifest(swapped, blobs, _pub(signer))


# -- missing & extra: blob set must equal manifest indices ------------------

@settings(max_examples=40, deadline=None)
@given(n=st.integers(min_value=1, max_value=5),
       drop_at=st.integers(min_value=0))
def test_dropping_any_blob_fails(n, drop_at):
    signer = SigningService()
    chain = [(f"l{i}", bytes([i + 1])) for i in range(n)]
    m, blobs = _seal_chain(
        signer, darwin_id="d", l0_version="1.0.0",
        generation=n - 1, layers=chain,
    )
    del blobs[drop_at % n]
    with pytest.raises(ManifestError):
        verify_manifest(m, blobs, _pub(signer))


@settings(max_examples=40, deadline=None)
@given(n=st.integers(min_value=1, max_value=5),
       extra_blob=_BLOB)
def test_an_extra_blob_fails(n, extra_blob):
    signer = SigningService()
    chain = [(f"l{i}", bytes([i + 1])) for i in range(n)]
    m, blobs = _seal_chain(
        signer, darwin_id="d", l0_version="1.0.0",
        generation=n - 1, layers=chain,
    )
    blobs[n] = extra_blob  # an index the manifest does not declare
    with pytest.raises(ManifestError):
        verify_manifest(m, blobs, _pub(signer))


# -- replay: a genuine layer minted for a different binding is rejected -----

@settings(max_examples=40, deadline=None)
@given(
    a_id=_ID, b_id=_ID, a_ver=_VER, b_ver=_VER, blob=_BLOB,
)
def test_replay_across_lineage_or_version_or_slot(a_id, b_id, a_ver, b_ver, blob):
    if (a_id, a_ver) == (b_id, b_ver):  # genuinely different binding required
        b_id = b_id + "x"
    signer = SigningService()
    other, _ = _seal_chain(
        signer, darwin_id=a_id, l0_version=a_ver, generation=0,
        layers=[("layer_0", blob)],
    )
    victim, victim_blobs = _seal_chain(
        signer, darwin_id=b_id, l0_version=b_ver, generation=0,
        layers=[("layer_0", blob)],
    )
    # Splice the *attacker's* signed entry into the victim's manifest.
    spliced = Manifest(
        identity=victim.identity, layers=[other.layers[0]],
        created_at=victim.created_at, signature=victim.signature,
    )
    with pytest.raises(ManifestError):
        verify_manifest(spliced, victim_blobs, _pub(signer))


# -- wrong key: any other signer's pubkey fails -----------------------------

@settings(max_examples=30, deadline=None)
@given(st.integers(min_value=1, max_value=4))
def test_any_other_pubkey_fails(n):
    signer = SigningService()
    attacker = SigningService()
    chain = [(f"l{i}", bytes([i])) for i in range(n)]
    m, blobs = _seal_chain(
        signer, darwin_id="d", l0_version="1.0.0",
        generation=n - 1, layers=chain,
    )
    with pytest.raises(ManifestError):
        verify_manifest(m, blobs, _pub(attacker))


# -- compaction invariant: len <= generation+1, never more ------------------

@settings(max_examples=60, deadline=None)
@given(
    layer_count=st.integers(min_value=1, max_value=5),
    generation=st.integers(min_value=0, max_value=10),
)
def test_layer_count_may_be_less_but_never_more_than_generation_plus_one(
    layer_count, generation
):
    signer = SigningService()
    chain = [(f"l{i}", bytes([i + 1])) for i in range(layer_count)]
    m, blobs = _seal_chain(
        signer, darwin_id="d", l0_version="1.0.0",
        generation=generation, layers=chain,
    )
    if layer_count <= generation + 1:
        # Uncompacted (==) or compacted (<) chains both verify.
        verify_manifest(m, blobs, _pub(signer))
    else:
        # Carrying more layers than reproductions is structurally impossible.
        with pytest.raises(ManifestError):
            verify_manifest(m, blobs, _pub(signer))


# -- bad public-key inputs are rejected at the trust-root entry -------------

@settings(max_examples=20, deadline=None)
@given(st.binary(min_size=0, max_size=300))
def test_arbitrary_bytes_are_not_a_valid_platform_pubkey(blob):
    # Refuses garbage and non-Ed25519 PEMs; never silently "verifies nothing".
    with pytest.raises(Exception):
        load_platform_public_key(blob)
