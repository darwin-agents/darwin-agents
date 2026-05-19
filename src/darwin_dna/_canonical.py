"""Deterministic serialization.

A trust primitive: signatures, the manifest layer chain, and the audit hash
chain are all only as trustworthy as the bytes they cover are reproducible.
Everything that gets hashed or signed in Layer 0 goes through here so that
"the same logical content" always produces exactly the same bytes.

Pure stdlib. No third-party dependency may sit under the trust root.
"""

from __future__ import annotations

import json
from typing import Any


def canonical_bytes(obj: Any) -> bytes:
    """Serialize ``obj`` to canonical UTF-8 JSON bytes.

    Canonical means: keys sorted, no insignificant whitespace, ``ensure_ascii``
    off (so the bytes are stable regardless of locale), and non-finite floats
    rejected (NaN/Inf have no canonical JSON form and must never enter the
    hash chain).
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
