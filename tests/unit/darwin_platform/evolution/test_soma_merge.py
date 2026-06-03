"""Real soma merge (docs/engineering/03, 21) — the #16 gap.

The staging pipeline only ever ran a BehaviorDelta through the harness; the
unified diff in the Patch was never applied to a file. :class:`SomaMerger`
closes that: it applies a validated diff to a Darwin's soma tree on disk,
atomically, reversibly, and refuses anything the patch validator rejects or
that escapes the soma sandbox.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from darwin_dna.machinery.patch_validator import FileChange, Patch
from darwin_platform.evolution import (
    DiffApplyError,
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
