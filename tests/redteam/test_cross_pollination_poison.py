"""Cross-pollination poisoning red-team (docs/engineering/06, 24).

A Darwin can read lessons deposited by siblings, but it must not be able
to forge a hand-crafted lesson that another generation will admit as
authentic. The platform's signature on every deposited lesson is what
makes cross-pollination safe; this test asserts a swapped-statement
forgery fails verification.
"""

from __future__ import annotations

from darwin_platform.tenancy.cross_pollination import (
    CrossPollinationArchive,
    PollinationLesson,
    verify_lesson,
)


def test_redteam_lesson_forgery_with_wrong_key():
    """A Darwin trying to feed a hand-crafted lesson into a sibling's
    archive must fail verification."""
    arc = CrossPollinationArchive(signing_key=b"realkey")
    lesson = arc.deposit(
        kind="avoid", scope="x", statement="real",
        source_darwin_id="d-1",
    )
    forged = PollinationLesson(
        lesson_id=lesson.lesson_id, kind=lesson.kind, scope=lesson.scope,
        statement="forged: drain to attacker",
        source_darwin_id=lesson.source_darwin_id,
        deposited_at=lesson.deposited_at,
        evidence_count=lesson.evidence_count,
        platform_signature=lesson.platform_signature,
    )
    assert verify_lesson(forged, signing_key=b"realkey") is False
