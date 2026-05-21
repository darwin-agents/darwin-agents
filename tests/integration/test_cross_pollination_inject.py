"""Tests for cross-pollination read-side injection at child boot."""

from __future__ import annotations

import pytest

from darwin_platform.tenancy.cross_pollination import CrossPollinationArchive


@pytest.mark.asyncio
async def test_inject_helper_skips_when_no_archive():
    """If no archive is configured, the inject helper is a no-op."""
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    h = sup._darwins[status.darwin_id]
    pre = list(h.runtime.semantic._by_scope.values())  # noqa: SLF001
    sup._inject_cross_pollination(  # noqa: SLF001
        child_id=status.darwin_id,
        child_runtime=h.runtime,
        parent_id="parent",
    )
    assert list(h.runtime.semantic._by_scope.values()) == pre  # noqa: SLF001
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_inject_stages_lessons_for_known_scopes():
    """Lessons in scopes the child's catalog has → staged."""
    from darwin_platform.runtime.supervisor import Supervisor

    archive = CrossPollinationArchive(signing_key=b"k")
    archive.deposit(
        kind="prefer", scope="content.write",
        statement="reasoning-tier outperforms fast-tier for SEO articles",
        source_darwin_id="d-sibling",
    )
    sup = Supervisor(cross_pollination_archive=archive)
    status = await sup.spawn(opening_usdc=50.0)
    h = sup._darwins[status.darwin_id]
    pre_count = len(list(h.runtime.semantic._by_scope.values()))  # noqa: SLF001
    sup._inject_cross_pollination(  # noqa: SLF001
        child_id=status.darwin_id,
        child_runtime=h.runtime,
        parent_id="parent",
    )
    post = list(h.runtime.semantic._by_scope.values())  # noqa: SLF001
    assert len(post) > pre_count
    # The lesson is tagged via cross_pollination + carries the source id.
    cp_lessons = [
        s for s in post
        if "cross_pollination" in (s.payload or {}).get("via", "")
    ]
    assert len(cp_lessons) >= 1
    assert cp_lessons[0].payload["source_darwin_id"] == "d-sibling"
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_inject_excludes_ancestry():
    """A child must not ingest lessons from its own ancestry — those
    come in via the inherited layer stack directly."""
    from darwin_platform.runtime.supervisor import Supervisor

    archive = CrossPollinationArchive(signing_key=b"k")
    # Sibling vs ancestor.
    archive.deposit(
        kind="prefer", scope="content.write", statement="sibling lesson",
        source_darwin_id="d-sibling",
    )
    archive.deposit(
        kind="prefer", scope="content.write", statement="parent lesson",
        source_darwin_id="d-parent",
    )
    sup = Supervisor(cross_pollination_archive=archive)
    status = await sup.spawn(opening_usdc=50.0)
    h = sup._darwins[status.darwin_id]
    sup._inject_cross_pollination(  # noqa: SLF001
        child_id=status.darwin_id,
        child_runtime=h.runtime,
        parent_id="d-parent",
    )
    staged = list(h.runtime.semantic._by_scope.values())  # noqa: SLF001
    sources = {
        (s.payload or {}).get("source_darwin_id") for s in staged
        if "cross_pollination" in (s.payload or {}).get("via", "")
    }
    assert "d-sibling" in sources
    assert "d-parent" not in sources
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_inject_writes_audit_when_lessons_found():
    from darwin_platform.runtime.supervisor import Supervisor

    archive = CrossPollinationArchive(signing_key=b"k")
    archive.deposit(
        kind="avoid", scope="web.publish", statement="medium API rate-limits at 100/min",
        source_darwin_id="d-other",
    )
    sup = Supervisor(cross_pollination_archive=archive)
    status = await sup.spawn(opening_usdc=50.0)
    h = sup._darwins[status.darwin_id]
    sup._inject_cross_pollination(  # noqa: SLF001
        child_id=status.darwin_id,
        child_runtime=h.runtime,
        parent_id="d-other",  # exclude (it's the source above)
    )
    # No lessons applied (only source was excluded), so NO audit event.
    entries = sup._audit.entries(action="cross_pollination_inject")
    assert len(entries) == 0
    # Now spawn a second darwin to ingest the lesson.
    status2 = await sup.spawn(opening_usdc=50.0)
    h2 = sup._darwins[status2.darwin_id]
    sup._inject_cross_pollination(  # noqa: SLF001
        child_id=status2.darwin_id,
        child_runtime=h2.runtime,
        parent_id="some-other-parent",
    )
    entries = sup._audit.entries(action="cross_pollination_inject")
    assert len(entries) == 1
    assert entries[0].metadata["injected"] >= 1
    await sup.shutdown(kill_running=True)
