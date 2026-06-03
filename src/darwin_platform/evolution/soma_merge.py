"""Real soma merge — promote a validated patch onto the live soma
(docs/engineering/03 §"apply_mutation", docs/engineering/21 §"Patch staging").

The Layer-0 genome decides *whether* to self-modify (propose → evaluate →
apply); it never writes code itself (Layer 0 is read-only for
self-reflection, P1). The actual file write is a **platform** concern,
exactly like :mod:`darwin_platform.evolution.patch_staging` — the genome's
``apply_mutation`` node reaches it through an optional ``ctx.soma_merge``
hook. Without the hook the node keeps the Phase-0 behavioural simulation
(clear the recurring-failure counter); with it, the validated unified diff
is applied to the Darwin's soma tree on disk and the fix only counts as
landed when the merge succeeded.

This closes the audit gap "``apply_mutation`` never merges a real patch —
self-modification is simulated": the staging pipeline only ever *ran* a
``BehaviorDelta`` through the harness; the diff in the :class:`Patch`
travelled for audit and was never applied anywhere.

Design constraints:

- **Independent re-validation.** The proposer's verdict is never trusted;
  :func:`~darwin_dna.machinery.patch_validator.validate_patch` runs again
  here so a patch that targets the genome / validator / kill-switch can't
  reach disk even if propose somehow let it through.
- **Sandboxed paths.** Every target is resolved under the soma root; a path
  that escapes (``..``, absolute, symlink-out) is refused — defence in
  depth below the validator's own path check.
- **All-or-nothing.** Every diff is *computed* before any file is written,
  so a malformed hunk in the second change aborts before the first is
  touched. A failure mid-write restores from the pre-merge snapshot.
- **Reversible.** A successful merge keeps the pre-merge contents under a
  ``snapshot_id`` so :meth:`SomaMerger.rollback` can undo it (the recovery
  path when a promoted patch later misbehaves in production).
- **stdlib only.** The diff applier is in-house (no ``git`` / ``patch``
  dependency) so it works in any sandbox image. It is deliberately
  *strict*: context and removed lines must match exactly. Fuzzy patching of
  self-modifying source is a foot-gun — a mismatch fails the merge (archived
  as a negative lesson) rather than silently corrupting the soma.
"""

from __future__ import annotations

import os
import posixpath
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from darwin_dna.machinery.patch_validator import Patch, validate_patch


class DiffApplyError(Exception):
    """A unified diff could not be applied to its target (bad header,
    context mismatch, removal mismatch, or overlapping hunks)."""


_HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)


@dataclass
class _Hunk:
    old_start: int
    lines: list[str]  # raw body lines, each prefixed ' ', '+', or '-'


def _parse_hunks(diff: str) -> list[_Hunk]:
    """Split unified-diff text into hunks. ``---``/``+++`` file headers and
    ``\\ No newline`` markers are ignored; everything between ``@@`` headers
    is a body line. Raises :class:`DiffApplyError` on a malformed header or
    a stray body line outside any hunk."""
    hunks: list[_Hunk] = []
    cur: _Hunk | None = None
    for raw in diff.splitlines():
        if raw.startswith("--- ") or raw.startswith("+++ "):
            continue
        if raw.startswith("@@"):
            m = _HUNK_RE.match(raw)
            if not m:
                raise DiffApplyError(f"malformed hunk header: {raw!r}")
            cur = _Hunk(old_start=int(m.group(1)), lines=[])
            hunks.append(cur)
            continue
        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        if cur is None:
            # A non-empty line before any hunk header → this isn't a real
            # unified diff (e.g. the Phase-0 fabricated "+ ..." placeholder).
            if raw.strip() == "":
                continue
            raise DiffApplyError("diff body before any @@ hunk header")
        # A bare empty line inside a hunk is a blank *context* line.
        cur.lines.append(raw if raw[:1] in (" ", "+", "-") else " " + raw)
    return hunks


def apply_unified_diff(original: str, diff: str, *, path: str = "") -> str:
    """Apply a strict unified diff to ``original`` and return the new text.

    Context (`` ``) and removed (``-``) lines must match ``original``
    exactly at the position the hunk header names; any mismatch raises
    :class:`DiffApplyError`. Supports new-file creation (``original == ""``
    with an all-``+`` hunk). Preserves a trailing newline when the original
    had one (and adds one for a freshly created non-empty file)."""
    hunks = _parse_hunks(diff)
    if not hunks:
        raise DiffApplyError(
            f"no @@ hunks in diff for {path or '<patch>'} — not a unified diff"
        )
    orig_lines = original.splitlines()
    out: list[str] = []
    src = 0  # 0-based cursor into orig_lines
    for h in hunks:
        start = max(0, h.old_start - 1)
        if start < src:
            raise DiffApplyError("overlapping or out-of-order hunks")
        if start > len(orig_lines):
            raise DiffApplyError("hunk starts past end of file")
        out.extend(orig_lines[src:start])
        src = start
        for body in h.lines:
            tag, content = body[0], body[1:]
            if tag == " ":
                if src >= len(orig_lines) or orig_lines[src] != content:
                    raise DiffApplyError(
                        f"context mismatch at {path}:{src + 1}"
                    )
                out.append(content)
                src += 1
            elif tag == "-":
                if src >= len(orig_lines) or orig_lines[src] != content:
                    raise DiffApplyError(
                        f"removal mismatch at {path}:{src + 1}"
                    )
                src += 1
            elif tag == "+":
                out.append(content)
    out.extend(orig_lines[src:])
    text = "\n".join(out)
    if text and (original.endswith("\n") or original == ""):
        text += "\n"
    return text


@dataclass(frozen=True)
class SomaMergeResult:
    """Outcome of a merge. The genome's apply_mutation node treats the fix
    as landed only when ``merged`` is True; ``reason`` is recorded on the
    audit log / mutation archive. ``snapshot_id`` ties a successful merge to
    its rollback handle."""

    merged: bool
    reason: str
    applied_paths: tuple[str, ...]
    snapshot_id: str
    failure_class: str = ""
    rejected_path: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "merged": self.merged,
            "reason": self.reason,
            "applied_paths": list(self.applied_paths),
            "snapshot_id": self.snapshot_id,
            "failure_class": self.failure_class,
            "rejected_path": self.rejected_path,
        }


class SomaMerger:
    """Apply a validated :class:`Patch` to a Darwin's soma tree on disk.

    One instance per soma root (in production, per Darwin — the soma is the
    organism's mutable code and must not be shared across tenants). The
    supervisor builds one per spawn and exposes it to the genome via the
    ``ctx.soma_merge`` hook.

    Every method returns a :class:`SomaMergeResult` and never raises on a
    rejected/failed patch — a self-modification failure must not crash the
    drive loop. Programmer errors (a bad soma root) still raise.
    """

    def __init__(
        self,
        soma_root: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        applier: Callable[..., str] = apply_unified_diff,
        snapshot_cap: int = 64,
    ) -> None:
        self._root = Path(soma_root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._applier = applier
        self._snapshot_cap = max(1, int(snapshot_cap))
        # snapshot_id -> {relpath: original_text_or_None(=file did not exist)}
        self._snapshots: dict[str, dict[str, str | None]] = {}

    @property
    def root(self) -> Path:
        return self._root

    # -- public entry point ------------------------------------------------
    def merge(self, patch: Patch, *, failure_class: str = "") -> SomaMergeResult:
        # 1. Independent re-validation (never trust the proposer).
        decision = validate_patch(patch)
        if not decision.ok:
            return SomaMergeResult(
                merged=False,
                reason=f"validator rejected: {decision.reason}",
                applied_paths=(),
                snapshot_id="",
                failure_class=failure_class,
                rejected_path=decision.rejected_path,
            )

        # 2. Resolve + sandbox-check every target path.
        targets: list[tuple[str, Path, str]] = []
        for change in patch.changes:
            rel = posixpath.normpath(change.path).lstrip("./")
            abspath = (self._root / rel).resolve()
            if not self._within_root(abspath):
                return SomaMergeResult(
                    merged=False,
                    reason="path escapes the soma sandbox",
                    applied_paths=(),
                    snapshot_id="",
                    failure_class=failure_class,
                    rejected_path=change.path,
                )
            targets.append((rel, abspath, change.diff))

        if not targets:
            return SomaMergeResult(
                merged=False, reason="patch has no changes",
                applied_paths=(), snapshot_id="",
                failure_class=failure_class,
            )

        # 3. Snapshot originals + compute ALL new contents before writing
        #    anything (all-or-nothing).
        originals: dict[str, str | None] = {}
        new_contents: dict[str, str] = {}
        try:
            for rel, abspath, diff in targets:
                originals[rel] = (
                    abspath.read_text() if abspath.is_file() else None
                )
                new_contents[rel] = self._applier(
                    originals[rel] or "", diff, path=rel
                )
        except DiffApplyError as exc:
            return SomaMergeResult(
                merged=False, reason=f"diff apply failed: {exc}",
                applied_paths=(), snapshot_id="",
                failure_class=failure_class,
            )

        # 4. Write atomically; restore from snapshot on any partial failure.
        try:
            for rel, abspath, _ in targets:
                abspath.parent.mkdir(parents=True, exist_ok=True)
                self._atomic_write(abspath, new_contents[rel])
        except Exception as exc:  # noqa: BLE001 — surface as a failed merge
            self._restore(originals)
            return SomaMergeResult(
                merged=False, reason=f"write failed (rolled back): {exc}",
                applied_paths=(), snapshot_id="",
                failure_class=failure_class,
            )

        # 5. Keep the pre-merge snapshot for rollback.
        snapshot_id = f"soma-{int(self._clock() * 1000)}"
        self._snapshots[snapshot_id] = originals
        if len(self._snapshots) > self._snapshot_cap:
            oldest = next(iter(self._snapshots))
            del self._snapshots[oldest]

        return SomaMergeResult(
            merged=True, reason="merged",
            applied_paths=tuple(sorted(originals)),
            snapshot_id=snapshot_id,
            failure_class=failure_class,
        )

    def rollback(self, snapshot_id: str) -> bool:
        """Restore the soma to its pre-merge state for ``snapshot_id``.
        Returns False for an unknown id. Idempotent best-effort."""
        snap = self._snapshots.get(snapshot_id)
        if snap is None:
            return False
        self._restore(snap)
        return True

    # -- helpers -----------------------------------------------------------
    def _within_root(self, abspath: Path) -> bool:
        root = str(self._root)
        target = str(abspath)
        return target == root or target.startswith(root + os.sep)

    def _atomic_write(self, abspath: Path, content: str) -> None:
        fd, tmp = tempfile.mkstemp(
            dir=str(abspath.parent), prefix=".soma-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(content)
            os.replace(tmp, abspath)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _restore(self, snapshot: dict[str, str | None]) -> None:
        for rel, original in snapshot.items():
            abspath = self._root / rel
            if original is None:
                # File did not exist before the merge — remove it.
                try:
                    abspath.unlink()
                except OSError:
                    pass
            else:
                try:
                    self._atomic_write(abspath, original)
                except OSError:
                    pass
