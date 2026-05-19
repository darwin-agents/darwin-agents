"""The layer stack: verify + compose inherited lessons (docs/engineering/02).

A lesson is *data*, not code (the open question in 02-dna-core.md, resolved on
the side of legibility): "platform X rate-limits before 09:00 UTC" is a
structured record the soma's orchestrator reads and adapts to. A 400-line
neuron rewrite is soma, never a sealed layer. Keeping layers declarative is
what lets a human — or a future generation — point at *which ancestor's lesson*
caused a behaviour.

The stack is read-only at runtime. The organism may *read* its DNA to reason
about itself; it may never write it (P1).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .._canonical import canonical_bytes
from ..manifest import Manifest, verify_manifest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

LessonKind = Literal[
    "avoid",  # don't do X (a learned hard-won negative)
    "prefer",  # when choosing, lean toward X
    "timing",  # X is time/rate sensitive
    "blacklist",  # never touch this counterparty/category
    "whitelist",  # this is known-good
    "calibration",  # an empirical constant (e.g. observed cost/latency)
]


class Lesson(BaseModel):
    """One distilled, sealed thing the lineage learned the hard way."""

    model_config = {"frozen": True}

    id: str
    kind: LessonKind
    scope: str  # e.g. "neuron:content.publisher", "strategy:content_affiliate"
    statement: str  # human-legible; the soma reads this
    payload: dict[str, Any] = Field(default_factory=dict)  # structured hint
    rationale: str  # *why* — so a human can audit the behaviour later
    born_generation: int = Field(ge=0)


class LayerStack:
    """The verified, composed view of every inherited layer, oldest first.

    Construct only via :meth:`load`, which runs the full trust-root check. A
    ``LayerStack`` you hold is a layer chain that *verified*.
    """

    def __init__(self, manifest: Manifest, lessons_by_layer: list[list[Lesson]]):
        self._manifest = manifest
        self._by_layer = lessons_by_layer  # index-aligned with manifest.layers

    @classmethod
    def load(
        cls,
        manifest: Manifest,
        layer_blobs: dict[int, bytes],
        platform_public_key: Ed25519PublicKey,
    ) -> "LayerStack":
        """Verify the chain, then parse each layer's lessons.

        Parsing happens *after* verification — we never deserialize attacker-
        controlled bytes we haven't authenticated.
        """
        verify_manifest(manifest, layer_blobs, platform_public_key)
        import json

        lessons_by_layer: list[list[Lesson]] = []
        for entry in manifest.layers:
            raw = json.loads(layer_blobs[entry.index].decode("utf-8"))
            if not isinstance(raw, list):
                raise ValueError(
                    f"layer {entry.index} payload must be a list of lessons"
                )
            lessons_by_layer.append([Lesson.model_validate(x) for x in raw])
        return cls(manifest, lessons_by_layer)

    @property
    def identity(self):
        return self._manifest.identity

    @property
    def generation(self) -> int:
        return self._manifest.identity.generation

    def all_lessons(self) -> list[Lesson]:
        """Every lesson, oldest layer first. Later (descendant) lessons come
        last so a consumer scanning forward sees the most recent guidance
        last and can let it win."""
        out: list[Lesson] = []
        for layer in self._by_layer:
            out.extend(layer)
        return out

    def find(
        self, *, kind: LessonKind | None = None, scope: str | None = None
    ) -> list[Lesson]:
        return [
            l
            for l in self.all_lessons()
            if (kind is None or l.kind == kind)
            and (scope is None or l.scope == scope)
        ]


def serialize_lessons(lessons: list[Lesson]) -> bytes:
    """Canonical bytes for a layer's lessons — what the platform hashes and
    signs, and what :meth:`LayerStack.load` re-hashes to detect tampering."""
    return canonical_bytes([l.model_dump() for l in lessons])


def deserialize_lessons(blob: bytes) -> list[Lesson]:
    """Parse a layer blob back into lessons. Used by the reproduction event's
    layer-compaction step (docs/engineering/06) to fold several already-sealed
    layers into one. Mirrors exactly the parse :meth:`LayerStack.load` does
    *after* verification — callers must only pass blobs they have verified."""
    import json

    raw = json.loads(blob.decode("utf-8"))
    if not isinstance(raw, list):
        raise ValueError("layer payload must be a list of lessons")
    return [Lesson.model_validate(x) for x in raw]
