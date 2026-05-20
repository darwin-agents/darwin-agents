"""The manifest: the signed layer chain (docs/engineering/18, "the trust root").

A Darwin's DNA is its lineage's commit history — an ordered stack of sealed
layers. The manifest is the platform's signed statement of *exactly which
layers, in which order, with which content* constitute this Darwin's genome.

``verify_manifest`` is the single chokepoint every boot passes through. The
properties it must guarantee, each of which has a paranoid test:

* a tampered layer fails verification (per-layer content hash)
* out-of-order layers fail validation (contiguous indices, oldest first)
* a missing layer fails validation (no gaps in 0..N)
* an extra layer not in the manifest fails validation
* a replayed valid-but-old layer fails (per-layer signature binds darwin_id +
  Layer 0 version + index, so a real layer from another lineage or version
  cannot be slotted in)

Defense in depth: the manifest signature alone already covers the whole layer
list; the per-layer signatures mean compromising the manifest signature is not
sufficient to swap a layer's bytes.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field

from ._canonical import canonical_bytes
from .signature import SignatureError, verify
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class ManifestError(Exception):
    """The manifest is structurally invalid or does not match the layer
    blobs. Boot must be refused."""


# By convention every platform-supplied amendment layer (doc 02) names
# itself with this prefix. The trust root excludes amendment-named layers
# from the layer-count cap (they are sanctioned platform additions, each
# individually signed). The per-layer signature remains the real defense:
# a junk layer named "layer_amendment_evil" would still fail to verify
# unless the platform itself signed it.
AMENDMENT_LAYER_PREFIX = "layer_amendment"


class LayerEntry(BaseModel):
    model_config = {"frozen": True}

    index: int = Field(ge=0)
    name: str
    sha256: str  # hex digest of the layer's canonical content bytes
    signature: str  # hex Ed25519 signature, per-layer (see signed_payload)


class ManifestIdentity(BaseModel):
    model_config = {"frozen": True}

    darwin_id: str
    generation: int = Field(ge=0)
    parent_id: str | None = None
    birth_ts: float
    lineage: list[str] = Field(default_factory=list)
    l0_version: str


class Manifest(BaseModel):
    model_config = {"frozen": True}

    identity: ManifestIdentity
    layers: list[LayerEntry]
    created_at: float
    signature: str  # hex Ed25519 signature over the manifest body (sans this)

    def _body(self) -> dict:
        """The bytes the platform signed: everything except ``signature``."""
        d = self.model_dump()
        d.pop("signature")
        return d


def layer_signed_payload(
    *, darwin_id: str, l0_version: str, index: int, name: str, sha256: str
) -> bytes:
    """The exact message the platform signs for one layer.

    It binds the content hash to *this lineage* and *this Layer 0 version* and
    *this position*. That binding is what makes a replay of a genuine,
    correctly-signed layer from a different Darwin (or a different L0 version,
    or a different slot) fail here.
    """
    return canonical_bytes(
        {
            "darwin_id": darwin_id,
            "l0_version": l0_version,
            "index": index,
            "name": name,
            "sha256": sha256,
        }
    )


def sha256_hex(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def verify_manifest(
    manifest: Manifest,
    layer_blobs: dict[int, bytes],
    platform_public_key: Ed25519PublicKey,
) -> Manifest:
    """Validate the whole chain or raise. Returns the manifest on success so
    callers can use it as a verified handle.

    ``layer_blobs`` maps layer index to that layer's canonical content bytes
    (the lessons payload). It must contain *exactly* the indices the manifest
    declares — no missing, no extra.
    """
    # 1. The manifest itself is authentic. Once this passes, the declared
    #    layer list, identity, and ordering are the platform's word.
    try:
        verify(
            platform_public_key,
            canonical_bytes(manifest._body()),
            bytes.fromhex(manifest.signature),
        )
    except (SignatureError, ValueError) as exc:
        raise ManifestError(f"manifest signature invalid: {exc}") from exc

    layers = manifest.layers
    if not layers:
        raise ManifestError("empty manifest: a Darwin has at least Layer 0")

    # 2. Ordering & completeness: layers must be exactly 0,1,...,N in order,
    #    with N == generation (generation k has k+1 layers, 0..k).
    indices = [layer.index for layer in layers]
    expected = list(range(len(layers)))
    if indices != expected:
        raise ManifestError(
            f"layer chain not contiguous/ordered oldest-first: "
            f"{indices} != {expected}"
        )
    # An uncompacted generation-k Darwin carries exactly k+1 layers (0..k).
    # Past the generation soft cap the reproduction event folds the oldest
    # layers into one (docs/engineering/06, 02), so a compacted lineage
    # carries *fewer* than generation+1 — never more. Contiguity (checked
    # above) plus the per-layer lineage-bound signatures (checked below) keep
    # the chain just as tamper/replay-proof; the count is only an upper bound.
    #
    # Amendment layers (doc 02 §amendment, doc 09): a forced reproduction
    # carrying a platform amendment adds an extra signed layer between the
    # inherited stack and the freshly distilled lessons. By convention the
    # amendment's name starts with ``layer_amendment``. Those layers do not
    # count toward the cap — they are sanctioned platform additions, each
    # individually signed by the platform key (verified below), so the per-
    # layer signature is the actual trust barrier here. A junk extra layer
    # without the prefix is still rejected by the count; a junk extra layer
    # WITH the prefix would still fail the per-layer signature check unless
    # the platform itself signed it.
    non_amendment = sum(
        1 for e in layers if not e.name.startswith(AMENDMENT_LAYER_PREFIX)
    )
    if non_amendment > manifest.identity.generation + 1:
        raise ManifestError(
            f"generation {manifest.identity.generation} carries "
            f"{non_amendment} non-amendment layers, more than the "
            f"{manifest.identity.generation + 1} its reproduction history allows"
        )

    # 3. No extra blobs, no missing blobs. An attacker who slips an extra
    #    signed layer into the bundle is rejected here even before hashing.
    if set(layer_blobs) != set(indices):
        raise ManifestError(
            f"layer blobs {sorted(layer_blobs)} do not match manifest "
            f"layers {indices} (missing or extra layer)"
        )

    # 4. Per-layer: content hash matches (tamper) and the per-layer signature
    #    binds it to this lineage + L0 version + slot (replay).
    for entry in layers:
        blob = layer_blobs[entry.index]
        digest = sha256_hex(blob)
        if digest != entry.sha256:
            raise ManifestError(
                f"layer {entry.index} ({entry.name}) content hash mismatch: "
                f"tampered or corrupted"
            )
        payload = layer_signed_payload(
            darwin_id=manifest.identity.darwin_id,
            l0_version=manifest.identity.l0_version,
            index=entry.index,
            name=entry.name,
            sha256=entry.sha256,
        )
        try:
            verify(platform_public_key, payload, bytes.fromhex(entry.signature))
        except (SignatureError, ValueError) as exc:
            raise ManifestError(
                f"layer {entry.index} ({entry.name}) signature invalid "
                f"(tamper, replay, or wrong lineage): {exc}"
            ) from exc

    return manifest
