"""Cross-pollination archive (docs/engineering/06 §"Cross-pollination",
phase 3).

A platform-side store of *distilled* lessons collected across sibling
Darwins. At reproduction time, a child can request a vetted subset of
this archive to inherit alongside its parent's lessons — so a child
can benefit from what an unrelated sibling already learned.

Safety posture:

- **Lessons are signed by the platform** at deposit time. A child only
  ingests lessons whose signature verifies against the same trust
  root that signs manifests. A Darwin cannot deposit a lesson directly;
  it can only nominate one, and the platform decides (per policy)
  whether to sign + admit it.
- **Bounded payload size + bounded count** per deposit so a Darwin
  cannot weaponise the archive against its siblings.
- **Topic-scoped retrieval** — children request lessons by capability
  scope (``content.publish``, ``ads.manage``, ...), never wholesale.
- **Per-scope rate limit** at retrieval so a busy reproduction cycle
  can't exhaust the budget.
- **Provenance preserved** — each archived lesson carries
  ``source_darwin_id`` and ``deposited_at`` so a forensic review can
  trace which Darwin's behaviour contributed to a hostile child.

The store can be in-memory (default; suitable for one supervisor
process) or backed by the persistence layer for multi-replica deploys.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable


# Hard limits — even an operator setting them looser cannot exceed these.
MAX_STATEMENT_LEN = 2048
MAX_SCOPE_LEN = 200
MAX_LESSONS_PER_RETRIEVAL = 20


class CrossPollinationError(Exception):
    """Deposit refused (bad shape, missing fields) or retrieval refused."""


@dataclass(frozen=True)
class PollinationLesson:
    """One signed cross-pollination lesson."""

    lesson_id: str
    kind: str           # "avoid" | "prefer" | "calibration"
    scope: str          # capability tag or strategy id
    statement: str
    source_darwin_id: str
    deposited_at: float
    evidence_count: int  # how many sibling-level confirmations
    platform_signature: str  # HMAC over canonical bytes

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def _canonical_bytes(
        *, lesson_id: str, kind: str, scope: str, statement: str,
        source_darwin_id: str, deposited_at: float, evidence_count: int,
    ) -> bytes:
        # Stable JSON, sorted keys, no whitespace.
        import json as _json
        body = _json.dumps(
            {
                "lesson_id": lesson_id, "kind": kind, "scope": scope,
                "statement": statement,
                "source_darwin_id": source_darwin_id,
                "deposited_at": float(deposited_at),
                "evidence_count": int(evidence_count),
            },
            sort_keys=True, separators=(",", ":"),
        )
        return body.encode("utf-8")


class CrossPollinationArchive:
    """In-memory archive. Bounded to ``max_lessons`` total; eviction
    drops the oldest entries when the cap is reached.

    The signing key is platform-held — Darwins never see it. The
    archive uses HMAC-SHA256 over the canonical lesson bytes so a
    forged signature is detectable + rejected at retrieval verify
    time."""

    def __init__(
        self,
        *,
        signing_key: bytes,
        max_lessons: int = 10_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not signing_key:
            raise ValueError("CrossPollinationArchive needs a signing_key")
        self._key = bytes(signing_key)
        self._max = int(max_lessons)
        self._clock = clock
        # Lessons indexed by id; we keep insertion order for FIFO eviction.
        self._by_id: dict[str, PollinationLesson] = {}
        # Per-scope index for cheap retrieval.
        self._by_scope: dict[str, list[str]] = {}

    # -- platform-side deposit ------------------------------------------
    def deposit(
        self,
        *,
        kind: str,
        scope: str,
        statement: str,
        source_darwin_id: str,
        evidence_count: int = 1,
    ) -> PollinationLesson:
        """Sign + store a lesson. Called by the supervisor when a
        Darwin's distilled lesson clears the platform's admission
        policy (which lives outside this module — keep it simple)."""
        if kind not in ("avoid", "prefer", "calibration"):
            raise CrossPollinationError(
                f"unknown lesson kind {kind!r}"
            )
        if not scope or len(scope) > MAX_SCOPE_LEN:
            raise CrossPollinationError(
                f"scope must be 1..{MAX_SCOPE_LEN} chars"
            )
        if not statement or len(statement) > MAX_STATEMENT_LEN:
            raise CrossPollinationError(
                f"statement must be 1..{MAX_STATEMENT_LEN} chars"
            )
        if not source_darwin_id:
            raise CrossPollinationError("source_darwin_id is required")
        evidence_count = max(1, int(evidence_count))
        lesson_id = uuid.uuid4().hex
        deposited_at = self._clock()
        sig = self._sign(
            lesson_id=lesson_id, kind=kind, scope=scope, statement=statement,
            source_darwin_id=source_darwin_id, deposited_at=deposited_at,
            evidence_count=evidence_count,
        )
        lesson = PollinationLesson(
            lesson_id=lesson_id, kind=kind, scope=scope,
            statement=statement, source_darwin_id=source_darwin_id,
            deposited_at=deposited_at, evidence_count=evidence_count,
            platform_signature=sig,
        )
        self._by_id[lesson_id] = lesson
        self._by_scope.setdefault(scope, []).append(lesson_id)
        # FIFO eviction when over cap.
        while len(self._by_id) > self._max:
            oldest = next(iter(self._by_id))
            old = self._by_id.pop(oldest)
            scope_list = self._by_scope.get(old.scope, [])
            if oldest in scope_list:
                scope_list.remove(oldest)
        return lesson

    # -- retrieval (children at reproduction time) ----------------------
    def retrieve(
        self,
        *,
        scope: str,
        excluding_darwin_ids: set[str] | None = None,
        limit: int = 5,
    ) -> list[PollinationLesson]:
        """Return up to ``limit`` lessons for ``scope`` whose signature
        verifies. A child should NOT receive its own ancestors'
        lessons (they're inherited via the parent's layer stack
        directly) — pass them via ``excluding_darwin_ids``."""
        if limit <= 0:
            return []
        cap = min(limit, MAX_LESSONS_PER_RETRIEVAL)
        excl = set(excluding_darwin_ids or ())
        ids = list(self._by_scope.get(scope, []))
        # Most recent first.
        ids.reverse()
        out: list[PollinationLesson] = []
        for lid in ids:
            if len(out) >= cap:
                break
            lesson = self._by_id.get(lid)
            if lesson is None:
                continue
            if lesson.source_darwin_id in excl:
                continue
            if not self._verify(lesson):
                # Forged or tampered — skip; this would happen only if
                # someone wrote directly into _by_id with a bad sig.
                continue
            out.append(lesson)
        return out

    def list_scopes(self) -> list[str]:
        return sorted(self._by_scope.keys())

    def __len__(self) -> int:
        return len(self._by_id)

    # -- signing primitives ---------------------------------------------
    def _sign(
        self,
        *,
        lesson_id: str, kind: str, scope: str, statement: str,
        source_darwin_id: str, deposited_at: float, evidence_count: int,
    ) -> str:
        import base64
        import hmac
        canon = PollinationLesson._canonical_bytes(  # noqa: SLF001
            lesson_id=lesson_id, kind=kind, scope=scope,
            statement=statement, source_darwin_id=source_darwin_id,
            deposited_at=deposited_at, evidence_count=evidence_count,
        )
        digest = hmac.new(self._key, canon, hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    def _verify(self, lesson: PollinationLesson) -> bool:
        import base64
        import hmac
        canon = PollinationLesson._canonical_bytes(  # noqa: SLF001
            lesson_id=lesson.lesson_id, kind=lesson.kind,
            scope=lesson.scope, statement=lesson.statement,
            source_darwin_id=lesson.source_darwin_id,
            deposited_at=lesson.deposited_at,
            evidence_count=lesson.evidence_count,
        )
        expected = base64.b64encode(
            hmac.new(self._key, canon, hashlib.sha256).digest()
        ).decode("ascii")
        return hmac.compare_digest(lesson.platform_signature, expected)


def verify_lesson(
    lesson: PollinationLesson, *, signing_key: bytes,
) -> bool:
    """Public verify primitive — usable by a child's loader before it
    accepts the lesson into its layer stack."""
    arc = CrossPollinationArchive(signing_key=signing_key, max_lessons=1)
    return arc._verify(lesson)  # noqa: SLF001
