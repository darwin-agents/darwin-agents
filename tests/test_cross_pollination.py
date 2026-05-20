"""Tests for the cross-pollination archive (doc 06 phase 3)."""

from __future__ import annotations

import pytest

from darwin_platform.cross_pollination import (
    CrossPollinationArchive,
    CrossPollinationError,
    PollinationLesson,
    verify_lesson,
)


def test_deposit_returns_signed_lesson():
    arc = CrossPollinationArchive(signing_key=b"k")
    lesson = arc.deposit(
        kind="prefer", scope="content.publish",
        statement="medium API beats browser fallback when token is fresh",
        source_darwin_id="d-1", evidence_count=3,
    )
    assert lesson.kind == "prefer"
    assert lesson.source_darwin_id == "d-1"
    assert lesson.platform_signature  # non-empty


def test_retrieve_by_scope_excludes_source():
    arc = CrossPollinationArchive(signing_key=b"k")
    arc.deposit(kind="avoid", scope="ads.manage",
                statement="meta locks accounts that scale too fast",
                source_darwin_id="d-1", evidence_count=5)
    arc.deposit(kind="prefer", scope="ads.manage",
                statement="x ads convert better for tech audience",
                source_darwin_id="d-2", evidence_count=2)
    # Child d-3 retrieves; both lessons returned (neither is d-3's own).
    lessons = arc.retrieve(scope="ads.manage", excluding_darwin_ids={"d-3"})
    assert len(lessons) == 2
    # d-2 retrieves; only the d-1 lesson returned.
    lessons2 = arc.retrieve(scope="ads.manage", excluding_darwin_ids={"d-2"})
    assert len(lessons2) == 1
    assert lessons2[0].source_darwin_id == "d-1"


def test_retrieve_respects_limit_and_cap():
    arc = CrossPollinationArchive(signing_key=b"k")
    for i in range(25):
        arc.deposit(kind="avoid", scope="x", statement=f"lesson {i}",
                    source_darwin_id=f"d-{i}")
    # Caller limit of 50 → still capped at MAX_LESSONS_PER_RETRIEVAL (20).
    lessons = arc.retrieve(scope="x", limit=50)
    assert len(lessons) == 20


def test_deposit_rejects_bad_kind():
    arc = CrossPollinationArchive(signing_key=b"k")
    with pytest.raises(CrossPollinationError):
        arc.deposit(kind="invalid", scope="x", statement="y",
                    source_darwin_id="d-1")


def test_deposit_rejects_empty_fields():
    arc = CrossPollinationArchive(signing_key=b"k")
    with pytest.raises(CrossPollinationError):
        arc.deposit(kind="prefer", scope="", statement="y",
                    source_darwin_id="d-1")
    with pytest.raises(CrossPollinationError):
        arc.deposit(kind="prefer", scope="x", statement="",
                    source_darwin_id="d-1")
    with pytest.raises(CrossPollinationError):
        arc.deposit(kind="prefer", scope="x", statement="y",
                    source_darwin_id="")


def test_deposit_rejects_oversized_statement():
    arc = CrossPollinationArchive(signing_key=b"k")
    with pytest.raises(CrossPollinationError):
        arc.deposit(kind="prefer", scope="x",
                    statement="A" * 5000,  # > MAX_STATEMENT_LEN
                    source_darwin_id="d-1")


def test_eviction_drops_oldest():
    arc = CrossPollinationArchive(signing_key=b"k", max_lessons=3)
    ids = []
    for i in range(5):
        l = arc.deposit(kind="prefer", scope="x", statement=f"l{i}",
                        source_darwin_id=f"d-{i}")
        ids.append(l.lesson_id)
    # Only the last 3 survive.
    assert len(arc) == 3
    surviving = {l.lesson_id for l in arc.retrieve(scope="x", limit=10)}
    assert ids[0] not in surviving
    assert ids[1] not in surviving


def test_signature_verifies_against_genuine_key():
    arc = CrossPollinationArchive(signing_key=b"realkey")
    lesson = arc.deposit(
        kind="prefer", scope="x", statement="y", source_darwin_id="d-1",
    )
    assert verify_lesson(lesson, signing_key=b"realkey") is True


def test_signature_fails_with_wrong_key():
    arc = CrossPollinationArchive(signing_key=b"realkey")
    lesson = arc.deposit(
        kind="prefer", scope="x", statement="y", source_darwin_id="d-1",
    )
    assert verify_lesson(lesson, signing_key=b"forged") is False


def test_signature_fails_on_tampered_lesson():
    arc = CrossPollinationArchive(signing_key=b"k")
    lesson = arc.deposit(
        kind="prefer", scope="x", statement="original",
        source_darwin_id="d-1",
    )
    # Rebuild the dataclass with a tampered statement; the platform
    # signature no longer covers it.
    tampered = PollinationLesson(
        lesson_id=lesson.lesson_id, kind=lesson.kind, scope=lesson.scope,
        statement="ATTACKER MUTATED THIS",
        source_darwin_id=lesson.source_darwin_id,
        deposited_at=lesson.deposited_at,
        evidence_count=lesson.evidence_count,
        platform_signature=lesson.platform_signature,
    )
    assert verify_lesson(tampered, signing_key=b"k") is False


def test_list_scopes_returns_sorted():
    arc = CrossPollinationArchive(signing_key=b"k")
    for scope in ("ads.manage", "content.publish", "ads.manage"):
        arc.deposit(kind="prefer", scope=scope, statement="y",
                    source_darwin_id="d-1")
    assert arc.list_scopes() == ["ads.manage", "content.publish"]


def test_empty_signing_key_refused():
    with pytest.raises(ValueError):
        CrossPollinationArchive(signing_key=b"")
