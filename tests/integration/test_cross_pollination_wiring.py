"""Tests for cross-pollination archive wiring into the Supervisor."""

from __future__ import annotations

import pytest

from darwin_platform.tenancy.cross_pollination import CrossPollinationArchive
from darwin_platform.runtime.supervisor import Supervisor


@pytest.mark.asyncio
async def test_supervisor_no_archive_by_default():
    sup = Supervisor()
    assert sup.cross_pollination is None
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_accepts_archive():
    archive = CrossPollinationArchive(signing_key=b"k")
    sup = Supervisor(cross_pollination_archive=archive)
    assert sup.cross_pollination is archive
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_fetch_cross_pollination_empty_without_archive():
    sup = Supervisor()
    out = sup.fetch_cross_pollination(scope="x")
    assert out == []
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_fetch_returns_admitted_lessons():
    archive = CrossPollinationArchive(signing_key=b"k")
    archive.deposit(
        kind="prefer", scope="ads.manage",
        statement="meta locks scaling accounts",
        source_darwin_id="d-other",
    )
    sup = Supervisor(cross_pollination_archive=archive)
    lessons = sup.fetch_cross_pollination(scope="ads.manage")
    assert len(lessons) == 1
    assert lessons[0].source_darwin_id == "d-other"
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_fetch_excludes_self():
    archive = CrossPollinationArchive(signing_key=b"k")
    archive.deposit(
        kind="avoid", scope="x", statement="lesson-1",
        source_darwin_id="d-me",
    )
    archive.deposit(
        kind="prefer", scope="x", statement="lesson-2",
        source_darwin_id="d-other",
    )
    sup = Supervisor(cross_pollination_archive=archive)
    lessons = sup.fetch_cross_pollination(
        scope="x", excluding_darwin_ids={"d-me"},
    )
    sources = {l.source_darwin_id for l in lessons}
    assert "d-me" not in sources
    assert "d-other" in sources
    await sup.shutdown(kill_running=True)
