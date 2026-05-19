"""Darwin identity & lineage metadata (docs/engineering/02-dna-core.md).

Identity is data, not behaviour. It is bound into the signed manifest so a
layer cannot be replayed into a different lineage (see ``manifest.py``).
"""

from __future__ import annotations

import os
import time
import uuid

from pydantic import BaseModel, Field


def uuid7() -> uuid.UUID:
    """An RFC 9562 UUIDv7 — time-ordered, so Darwin IDs sort by birth.

    Python 3.12's ``uuid`` module has no v7 yet, and Layer 0 may not reach
    outside the stdlib for the trust root, so we lay the bytes out by hand:
    48-bit big-endian Unix-ms timestamp, version nibble, 74 random bits with
    the RFC variant.
    """
    unix_ms = time.time_ns() // 1_000_000
    rand = bytearray(os.urandom(10))
    b = unix_ms.to_bytes(6, "big") + bytes(rand)
    b = bytearray(b)
    b[6] = 0x70 | (b[6] & 0x0F)  # version 7
    b[8] = 0x80 | (b[8] & 0x3F)  # RFC 4122/9562 variant
    return uuid.UUID(bytes=bytes(b))


class Identity(BaseModel):
    """Who this Darwin is and where it came from.

    Frozen: identity is established at birth by the platform and is never
    rewritten by the running organism.
    """

    model_config = {"frozen": True}

    darwin_id: str
    generation: int = Field(ge=0)
    parent_id: str | None = None
    birth_ts: float
    lineage: list[str] = Field(default_factory=list)  # ancestor ids, oldest first
    l0_version: str

    @classmethod
    def genesis(cls, *, l0_version: str) -> "Identity":
        """A fresh generation-0 identity. No parent, empty lineage."""
        return cls(
            darwin_id=str(uuid7()),
            generation=0,
            parent_id=None,
            birth_ts=time.time(),
            lineage=[],
            l0_version=l0_version,
        )

    def child(self, *, l0_version: str) -> "Identity":
        """The identity a reproduction event would mint for a child."""
        return Identity(
            darwin_id=str(uuid7()),
            generation=self.generation + 1,
            parent_id=self.darwin_id,
            birth_ts=time.time(),
            lineage=[*self.lineage, self.darwin_id],
            l0_version=l0_version,
        )
