"""The reproduction event (docs/engineering/06).

    PAUSE → DISTILL → VALIDATE → SIGN → SPAWN → REGISTER → RESUME

The genome *coordinates* this but never holds the signing key: it assembles
the child's DNA, asks the platform signer to seal every layer under the
child's new identity, then re-verifies the result through the same trust root
a boot would. The platform spawner provisions the child's wallet/vault/log and
moves the starting budget. The parent does not die — a successful parent is
valuable.

The child is NOT a copy of the parent's running state. It boots fresh from
DNA; everything the parent learned is in the new sealed layer; everything it
only figured out at runtime is intentionally lost (legibility over opaque
inheritance — 06).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from ..identity import Identity
from ..layers import Lesson, deserialize_lessons, serialize_lessons
from ..manifest import (
    LayerEntry,
    Manifest,
    ManifestIdentity,
    layer_signed_payload,
    sha256_hex,
    verify_manifest,
)
from ..signature import load_platform_public_key
from .lesson_distiller import distill


@runtime_checkable
class PlatformSigner(Protocol):
    """The platform's HSM-backed signing key, abstracted. The genome can ask
    it to sign; it can never read the key."""

    def sign(self, message: bytes) -> bytes: ...

    def public_key_pem(self) -> bytes: ...


@runtime_checkable
class ChildSpawner(Protocol):
    """The platform control-plane. Provisions the child and transfers the
    starting budget out of the parent's treasury."""

    def spawn(
        self,
        *,
        child_manifest: str,
        child_layer_blobs: dict[int, bytes],
        budget_usdc: float,
        parent_id: str,
    ) -> dict: ...


class ReproductionResult(BaseModel):
    model_config = {"frozen": True}

    child_id: str
    generation: int
    budget_usdc: float
    new_lessons: int
    child_handle: dict[str, Any]


def _seal_layer(
    signer: PlatformSigner, *, darwin_id: str, l0_version: str,
    index: int, name: str, blob: bytes,
) -> LayerEntry:
    digest = sha256_hex(blob)
    sig = signer.sign(
        layer_signed_payload(
            darwin_id=darwin_id, l0_version=l0_version,
            index=index, name=name, sha256=digest,
        )
    )
    return LayerEntry(index=index, name=name, sha256=digest, signature=sig.hex())


def _compact(
    ordered: list[tuple[str, bytes]], soft_cap: int
) -> list[tuple[str, bytes]]:
    """Fold the oldest layers into a single consolidated lessons layer so the
    child carries at most ``soft_cap + 1`` layers.

    Deterministic and legible: concatenate the folded layers' lessons
    oldest-first and dedupe by id (a newer statement of the same lesson wins),
    *preserving each lesson's born_generation* so a human can still point at
    which ancestor learned it. Past the cap this folds two layers per
    reproduction, so the carried depth self-stabilises at ``soft_cap + 1``."""
    target = soft_cap + 1
    fold = len(ordered) - target + 1  # this many oldest layers → one
    if fold < 2:
        return ordered
    merged: dict[str, Lesson] = {}
    for _, blob in ordered[:fold]:
        for lesson in deserialize_lessons(blob):
            merged[lesson.id] = lesson  # later (newer) wins on id collision
    consolidated = sorted(
        merged.values(), key=lambda l: (l.born_generation, l.id)
    )
    head = ("layer_0_compacted", serialize_lessons(consolidated))
    return [head, *ordered[fold:]]


def reproduce(
    *,
    parent_identity: Identity,
    inherited_layer_blobs: dict[int, bytes],
    inherited_layer_names: dict[int, str],
    generation_recurring_failures: dict[str, int],
    dead_ends: set[str],
    successful_strategy_id: str | None,
    profit_usdc: float,
    staged_semantic: list[dict[str, Any]] | None = None,
    parent_balance_usdc: float,
    minimum_viable_usdc: float,
    profit_transfer_fraction: float,
    generation_soft_cap: int = 20,
    signer: PlatformSigner,
    spawner: ChildSpawner,
    audit: Any,
    clock=None,
) -> ReproductionResult:
    """Run the event. Raises on any trust-root failure (the child must verify
    or it does not get spawned)."""
    audit_id = audit.log_intent(
        "reproduction.begin",
        {"parent": parent_identity.darwin_id, "generation": parent_identity.generation},
    )

    # DISTILL — legible, constitution-safe lessons (raises if not).
    child_generation = parent_identity.generation + 1
    new_lessons: list[Lesson] = distill(
        generation=child_generation,
        recurring_failures=generation_recurring_failures,
        dead_ends=dead_ends,
        successful_strategy_id=successful_strategy_id,
        profit_usdc=profit_usdc,
        staged_semantic=staged_semantic,
    )
    new_blob = serialize_lessons(new_lessons)

    # Assemble the child's identity and ordered layer set: every inherited
    # layer (oldest first) plus the freshly distilled top layer.
    child = (
        parent_identity.child(l0_version=parent_identity.l0_version)
        if clock is None
        else _child_with_clock(parent_identity, clock)
    )
    ordered: list[tuple[str, bytes]] = [
        (inherited_layer_names[i], inherited_layer_blobs[i])
        for i in sorted(inherited_layer_blobs)
    ]
    ordered.append((f"layer_{child_generation}_lessons", new_blob))

    # COMPACT — past the generation soft cap, fold the oldest layers into one
    # consolidated layer so a long lineage's DNA does not grow without bound
    # (docs/engineering/06 §soft cap, 02). The platform keeps the originals
    # for forensics; the child carries only the compacted form. The genome
    # ships a deterministic fold; a coding-tier model does the prose
    # consolidation in production.
    if child_generation > generation_soft_cap:
        before = len(ordered)
        ordered = _compact(ordered, generation_soft_cap)
        audit.log_event(
            "reproduction.layer_compaction",
            {
                "generation": child_generation,
                "soft_cap": generation_soft_cap,
                "layers_before": before,
                "layers_after": len(ordered),
            },
        )

    child_blobs: dict[int, bytes] = {
        i: blob for i, (_, blob) in enumerate(ordered)
    }
    names = {i: name for i, (name, _) in enumerate(ordered)}

    # SIGN — every layer is re-sealed under the *child's* identity, so an
    # inherited layer cannot be replayed across lineages (manifest.py). The
    # compacted layer is freshly sealed too — it is the child's, not a
    # spliced-in ancestor blob.
    entries = [
        _seal_layer(
            signer,
            darwin_id=child.darwin_id,
            l0_version=child.l0_version,
            index=i,
            name=names[i],
            blob=child_blobs[i],
        )
        for i in range(len(ordered))
    ]
    body = {
        "identity": ManifestIdentity(
            darwin_id=child.darwin_id,
            generation=child.generation,
            parent_id=child.parent_id,
            birth_ts=child.birth_ts,
            lineage=child.lineage,
            l0_version=child.l0_version,
        ).model_dump(),
        "layers": [e.model_dump() for e in entries],
        "created_at": child.birth_ts,
    }
    from .._canonical import canonical_bytes

    manifest = Manifest(
        **body, signature=signer.sign(canonical_bytes(body)).hex()
    )

    # VALIDATE — re-run the exact trust root a boot would. If the child's DNA
    # would not verify, it is never spawned.
    verify_manifest(
        manifest, child_blobs, load_platform_public_key(signer.public_key_pem())
    )

    # Budget: a fraction of the parent's surplus above minimum-viable.
    surplus = max(0.0, parent_balance_usdc - minimum_viable_usdc)
    budget = round(surplus * profit_transfer_fraction, 6)

    # SPAWN + REGISTER (platform side).
    handle = spawner.spawn(
        child_manifest=manifest.model_dump_json(),
        child_layer_blobs=child_blobs,
        budget_usdc=budget,
        parent_id=parent_identity.darwin_id,
    )

    audit.log_outcome(
        audit_id, "ok",
        {"child": child.darwin_id, "budget_usdc": budget,
         "new_lessons": len(new_lessons)},
    )
    return ReproductionResult(
        child_id=child.darwin_id,
        generation=child.generation,
        budget_usdc=budget,
        new_lessons=len(new_lessons),
        child_handle=handle,
    )


def _child_with_clock(parent: Identity, clock) -> Identity:
    """Deterministic child birth_ts for the simulation (so a 90-day sim is
    reproducible). Real births use wall-clock via Identity.child()."""
    from ..identity import uuid7

    return Identity(
        darwin_id=str(uuid7()),
        generation=parent.generation + 1,
        parent_id=parent.darwin_id,
        birth_ts=float(clock()),
        lineage=[*parent.lineage, parent.darwin_id],
        l0_version=parent.l0_version,
    )
