"""Real soma merge (docs/engineering/03, 21) — the #16 gap.

The staging pipeline only ever ran a BehaviorDelta through the harness; the
unified diff in the Patch was never applied to a file. :class:`SomaMerger`
closes that: it applies a validated diff to a Darwin's soma tree on disk,
atomically, reversibly, and refuses anything the patch validator rejects or
that escapes the soma sandbox.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from darwin_dna.machinery.patch_validator import FileChange, Patch
from darwin_platform.evolution import (
    DiffApplyError,
    SomaForensicSnapshot,
    SomaMerger,
    apply_unified_diff,
)


# -- the strict unified-diff applier --------------------------------------
def test_apply_modifies_existing_file_via_hunk():
    orig = "def f():\n    return 1\n"
    diff = (
        "--- a/x.py\n+++ b/x.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def f():\n"
        "-    return 1\n"
        "+    # backoff\n"
        "+    return 2\n"
    )
    assert apply_unified_diff(orig, diff, path="x.py") == (
        "def f():\n    # backoff\n    return 2\n"
    )


def test_apply_creates_new_file():
    diff = "--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,2 @@\n+a\n+b\n"
    assert apply_unified_diff("", diff, path="n.py") == "a\nb\n"


def test_apply_preserves_no_trailing_newline():
    orig = "a\nb"  # no trailing newline
    diff = "@@ -1,2 +1,2 @@\n a\n-b\n+B\n"
    assert apply_unified_diff(orig, diff, path="x") == "a\nB"


def test_apply_rejects_context_mismatch():
    with pytest.raises(DiffApplyError):
        apply_unified_diff("x\ny\n", "@@ -1,1 +1,1 @@\n WRONG\n", path="z")


def test_apply_rejects_removal_mismatch():
    with pytest.raises(DiffApplyError):
        apply_unified_diff("keep\n", "@@ -1,1 +1,0 @@\n-gone\n", path="z")


def test_apply_rejects_diff_without_hunks():
    # The Phase-0 fabricated placeholder ("+ ...", no @@ header) is not a
    # real unified diff and must not be applied.
    with pytest.raises(DiffApplyError):
        apply_unified_diff("a\n", "+    pass\n", path="z")


# -- SomaMerger ------------------------------------------------------------
def _seed(root: Path) -> None:
    (root / "soma" / "strategies").mkdir(parents=True)
    (root / "soma" / "strategies" / "x.py").write_text(
        "def f():\n    return 1\n"
    )


_GOOD_DIFF = (
    "--- a/soma/strategies/x.py\n+++ b/soma/strategies/x.py\n"
    "@@ -1,2 +1,3 @@\n def f():\n-    return 1\n+    # backoff\n+    return 2\n"
)


def test_merge_applies_and_is_reversible(tmp_path):
    _seed(tmp_path)
    m = SomaMerger(tmp_path)
    patch = Patch(
        rationale="fix",
        changes=[FileChange(path="soma/strategies/x.py", diff=_GOOD_DIFF)],
    )
    res = m.merge(patch, failure_class="flaky_io")
    assert res.merged is True
    assert res.applied_paths == ("soma/strategies/x.py",)
    assert res.snapshot_id
    assert (tmp_path / "soma" / "strategies" / "x.py").read_text() == (
        "def f():\n    # backoff\n    return 2\n"
    )
    # Rollback restores the original byte-for-byte.
    assert m.rollback(res.snapshot_id) is True
    assert (tmp_path / "soma" / "strategies" / "x.py").read_text() == (
        "def f():\n    return 1\n"
    )
    assert m.rollback("nope") is False


def test_merge_refuses_dna_path(tmp_path):
    m = SomaMerger(tmp_path)
    patch = Patch(
        rationale="x",
        changes=[FileChange(path="src/darwin_dna/x.py", diff=_GOOD_DIFF)],
    )
    res = m.merge(patch)
    assert res.merged is False
    assert "validator rejected" in res.reason
    assert res.rejected_path == "src/darwin_dna/x.py"


def test_merge_refuses_path_escape(tmp_path):
    # The validator accepts a path string under a soma-writable subtree, but
    # the merger's own sandbox check must still refuse it when the resolved
    # path leaves the root via a symlinked subtree — defence in depth below
    # the validator's string-level check.
    m = SomaMerger(tmp_path)
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    # soma/strategies is a writable subtree per the validator, but here it
    # points outside the soma root.
    (tmp_path / "soma").mkdir(exist_ok=True)
    (tmp_path / "soma" / "strategies").symlink_to(outside)
    new_file = "--- /dev/null\n+++ b/soma/strategies/evil.py\n@@ -0,0 +1,1 @@\n+x\n"
    patch = Patch(
        rationale="x",
        changes=[FileChange(path="soma/strategies/evil.py", diff=new_file)],
    )
    res = m.merge(patch)
    assert res.merged is False
    assert "escapes the soma sandbox" in res.reason
    # And nothing was written outside the root.
    assert not (outside / "evil.py").exists()


def test_merge_rejects_empty_patch(tmp_path):
    m = SomaMerger(tmp_path)
    res = m.merge(Patch(rationale="x", changes=[]))
    assert res.merged is False


def test_merge_is_all_or_nothing(tmp_path):
    # Two changes: the first applies, the second is malformed. Nothing must
    # be written (the first file stays untouched).
    _seed(tmp_path)
    (tmp_path / "soma" / "strategies" / "y.py").write_text("keep\n")
    m = SomaMerger(tmp_path)
    bad = "@@ -1,1 +1,1 @@\n-NOPE\n+changed\n"  # context won't match "keep"
    patch = Patch(
        rationale="x",
        changes=[
            FileChange(path="soma/strategies/x.py", diff=_GOOD_DIFF),
            FileChange(path="soma/strategies/y.py", diff=bad),
        ],
    )
    res = m.merge(patch)
    assert res.merged is False
    assert "diff apply failed" in res.reason
    # First file untouched — the merge computed all diffs before writing.
    assert (tmp_path / "soma" / "strategies" / "x.py").read_text() == (
        "def f():\n    return 1\n"
    )


# -- death-time forensic snapshot -----------------------------------------
def test_forensic_snapshot_captures_whole_tree(tmp_path):
    # The merger's rollback snapshots only hold patch *targets*; the death
    # snapshot must capture every file in the soma, mutated or not.
    soma = tmp_path / "soma_root" / "darwin-x"
    soma.mkdir(parents=True)
    _seed(soma)
    (soma / "soma" / "strategies" / "untouched.py").write_text("VALUE = 7\n")
    m = SomaMerger(soma, clock=lambda: 1234.5)

    snap = m.forensic_snapshot(reason="bankruptcy", darwin_id="darwin-x")

    assert isinstance(snap, SomaForensicSnapshot)
    assert snap.captured is True
    assert snap.reason == "bankruptcy"
    assert snap.darwin_id == "darwin-x"
    assert snap.snapshot_id == "death-1234500"
    assert snap.file_count == 2
    assert snap.digest  # a non-empty fingerprint over the tree
    assert snap.failure == ""

    # The artifact is persisted as a self-describing manifest with both file
    # metadata and full contents — outside the soma tree (a sibling root).
    out = Path(snap.path)
    assert out.is_file()
    assert soma not in out.parents
    manifest = json.loads(out.read_text())
    assert manifest["schema"] == "darwin-platform.soma-forensic/1"
    assert manifest["soma_digest"] == snap.digest
    assert (
        manifest["contents"]["soma/strategies/untouched.py"] == "VALUE = 7\n"
    )
    assert manifest["files"]["soma/strategies/x.py"]["sha256"] == (
        hashlib.sha256(b"def f():\n    return 1\n").hexdigest()
    )


def test_forensic_snapshot_digest_is_deterministic_and_changes_with_soma(
    tmp_path,
):
    soma = tmp_path / "darwin-y"
    _seed(soma)
    ticks = iter([1.0, 2.0, 3.0])
    m = SomaMerger(soma, clock=lambda: next(ticks))

    first = m.forensic_snapshot(reason="d1")
    # Same bytes on disk → identical fingerprint, different snapshot id.
    second = m.forensic_snapshot(reason="d2")
    assert second.digest == first.digest
    assert second.snapshot_id != first.snapshot_id

    # Mutate the soma → the fingerprint moves.
    (soma / "soma" / "strategies" / "x.py").write_text("def f():\n    return 9\n")
    third = m.forensic_snapshot(reason="d3")
    assert third.digest != first.digest


def test_forensic_snapshot_on_empty_soma_still_captures(tmp_path):
    soma = tmp_path / "darwin-empty"
    m = SomaMerger(soma, clock=lambda: 5.0)
    snap = m.forensic_snapshot(reason="death")
    assert snap.captured is True
    assert snap.file_count == 0
    assert snap.total_bytes == 0
    # An empty tree fingerprints to the SHA-256 of nothing.
    assert snap.digest == hashlib.sha256().hexdigest()
    assert Path(snap.path).is_file()


def test_forensic_snapshot_records_live_mutation_history(tmp_path):
    _seed(tmp_path)
    m = SomaMerger(tmp_path)
    patch = Patch(
        rationale="fix",
        changes=[FileChange(path="soma/strategies/x.py", diff=_GOOD_DIFF)],
    )
    res = m.merge(patch)
    assert res.merged is True
    snap = m.forensic_snapshot(reason="death")
    manifest = json.loads(Path(snap.path).read_text())
    # The in-memory rollback snapshot ids are named in the death manifest —
    # the last record that the mutation history existed.
    assert res.snapshot_id in manifest["mutation_snapshot_ids"]


def test_forensic_snapshot_never_raises_on_write_failure(tmp_path):
    _seed(tmp_path)
    # Point the forensics root at a path blocked by an existing *file* so the
    # mkdir/write fails — capture must report the failure, not raise.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir\n")
    m = SomaMerger(tmp_path, forensics_root=blocker / "under-a-file")
    snap = m.forensic_snapshot(reason="death")
    assert snap.captured is False
    assert snap.path is None
    assert snap.failure  # carries the cause


def test_forensic_snapshot_skips_forensics_dir_under_root(tmp_path):
    # If a caller roots forensics inside the soma tree, the capture must not
    # fold prior snapshots into later ones (no runaway growth).
    _seed(tmp_path)
    m = SomaMerger(
        tmp_path, forensics_root=tmp_path / ".forensics", clock=lambda: 1.0,
    )
    first = m.forensic_snapshot(reason="d1")
    assert first.captured is True
    files_after_first = first.file_count
    m2 = SomaMerger(
        tmp_path, forensics_root=tmp_path / ".forensics", clock=lambda: 2.0,
    )
    second = m2.forensic_snapshot(reason="d2")
    # The second capture sees the same soma files, not the first manifest.
    assert second.file_count == files_after_first
