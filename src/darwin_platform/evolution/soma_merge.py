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

import hashlib
import json
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

# A forensic snapshot id is ``death-<ms>`` (see ``forensic_snapshot``). Reads
# validate against this so a caller-supplied id can never path-traverse out of
# the forensics root.
_FORENSIC_ID_RE = re.compile(r"^death-\d+$")

# Upper bound on a pod-supplied (k8s) forensic manifest the supervisor will
# persist. The genome's signed layer manifest is a few KB; 4 MiB is generous
# headroom while still refusing a runaway/compromised pod from filling disk.
_EXTERNAL_FORENSIC_MAX_BYTES = 4 * 1024 * 1024


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


@dataclass(frozen=True)
class SomaForensicSnapshot:
    """Outcome of a death-time forensic capture of the whole soma tree.

    ``merge`` keeps *mutation-time* rollback snapshots in memory during a
    Darwin's life (one per applied patch, evictable, gone when the organism
    dies). This is the complementary *death-time* artifact: the entire soma
    tree, persisted to disk under the forensics root, so an operator doing a
    post-mortem can read exactly what code was running at the moment of
    death — independent of whether any mutation was ever applied.

    ``digest`` is a fingerprint over every ``(relpath, sha256(bytes))`` pair
    in deterministic path order, so two snapshots can be compared (or a
    leaked soma matched back to a death) without diffing file-by-file.
    ``captured`` is False with ``failure`` set when the capture could not be
    written — forensic capture is best-effort and never raises on the death
    path."""

    captured: bool
    reason: str
    snapshot_id: str
    path: str | None
    darwin_id: str
    file_count: int
    total_bytes: int
    captured_at: float
    digest: str
    failure: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "captured": self.captured,
            "reason": self.reason,
            "snapshot_id": self.snapshot_id,
            "path": self.path,
            "darwin_id": self.darwin_id,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "captured_at": self.captured_at,
            "digest": self.digest,
            "failure": self.failure,
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
        forensics_root: str | Path | None = None,
    ) -> None:
        self._root = Path(soma_root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._applier = applier
        self._snapshot_cap = max(1, int(snapshot_cap))
        # snapshot_id -> {relpath: original_text_or_None(=file did not exist)}
        self._snapshots: dict[str, dict[str, str | None]] = {}
        # Where death-time forensic snapshots are persisted. Default is a
        # sibling of the per-Darwin soma root (``<soma_root>/_forensics/<id>``)
        # so the artifact survives the merger object and never pollutes the
        # organism's own tree. Created lazily on first capture.
        self._forensics_root = (
            Path(forensics_root).resolve()
            if forensics_root is not None
            else self._root.parent / "_forensics" / self._root.name
        )

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

    # -- death-time forensics ---------------------------------------------
    def forensic_snapshot(
        self,
        *,
        reason: str,
        darwin_id: str = "",
        captured_at: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SomaForensicSnapshot:
        """Capture the *entire* soma tree as it stands right now and persist
        it for post-mortem forensics. Called from the death sequence (doc 05)
        when a Darwin dies or is killed.

        Unlike :meth:`merge`'s in-memory rollback snapshots — which only hold
        the files a patch *touched* and vanish with the organism — this walks
        every file under the soma root, records each one's content and SHA-256,
        and writes a self-describing JSON manifest to the forensics root. The
        manifest is keyed by a millisecond ``snapshot_id`` and carries a
        ``soma_digest`` fingerprint over the whole tree.

        Never raises: a capture failure returns ``captured=False`` with the
        cause in ``failure`` rather than crashing the death/kill path."""
        ts = float(self._clock() if captured_at is None else captured_at)
        snapshot_id = f"death-{int(ts * 1000)}"
        try:
            contents: dict[str, str | None] = {}
            file_meta: dict[str, dict[str, Any]] = {}
            total_bytes = 0
            fingerprint = hashlib.sha256()
            for abspath in sorted(
                p for p in self._root.rglob("*") if p.is_file()
            ):
                # Never fold the forensics store into its own snapshot if a
                # caller rooted it under the soma tree.
                if self._forensics_root in abspath.parents:
                    continue
                rel = abspath.relative_to(self._root).as_posix()
                raw = abspath.read_bytes()
                total_bytes += len(raw)
                sha = hashlib.sha256(raw).hexdigest()
                fingerprint.update(rel.encode("utf-8"))
                fingerprint.update(sha.encode("ascii"))
                try:
                    text: str | None = raw.decode("utf-8")
                except UnicodeDecodeError:
                    # A non-UTF-8 soma file (unexpected for source) — keep the
                    # integrity hash but don't inline undecodable bytes.
                    text = None
                contents[rel] = text
                file_meta[rel] = {
                    "sha256": sha, "bytes": len(raw), "binary": text is None,
                }

            soma_digest = fingerprint.hexdigest()
            manifest = {
                "schema": "darwin-platform.soma-forensic/1",
                "snapshot_id": snapshot_id,
                "darwin_id": darwin_id,
                "reason": reason,
                "captured_at": ts,
                "soma_root": str(self._root),
                "file_count": len(contents),
                "total_bytes": total_bytes,
                "soma_digest": soma_digest,
                # The mutation history that was live at death — its rollback
                # snapshots are in-memory only, so naming them here is the
                # last record that they existed.
                "mutation_snapshot_ids": list(self._snapshots),
                "metadata": dict(metadata or {}),
                "files": file_meta,
                "contents": contents,
            }
            self._forensics_root.mkdir(parents=True, exist_ok=True)
            out = self._forensics_root / f"{snapshot_id}.json"
            self._atomic_write(
                out, json.dumps(manifest, indent=2, sort_keys=True)
            )
            return SomaForensicSnapshot(
                captured=True, reason=reason, snapshot_id=snapshot_id,
                path=str(out), darwin_id=darwin_id,
                file_count=len(contents), total_bytes=total_bytes,
                captured_at=ts, digest=soma_digest,
            )
        except Exception as exc:  # noqa: BLE001 — capture must never crash death
            return SomaForensicSnapshot(
                captured=False, reason=reason, snapshot_id=snapshot_id,
                path=None, darwin_id=darwin_id, file_count=0,
                total_bytes=0, captured_at=ts, digest="",
                failure=f"{type(exc).__name__}: {exc}",
            )

    def record_external_forensic(
        self, manifest: dict[str, Any], *, captured_at: float | None = None,
    ) -> SomaForensicSnapshot:
        """Persist a forensic manifest captured ELSEWHERE so it surfaces
        through :meth:`list_forensic_snapshots` / :meth:`read_forensic_snapshot`
        exactly like an in-process capture. A k8s pod has no mutable soma tree,
        so it computes its own code-fingerprint manifest and ships it via the
        control API; the supervisor writes it here. A missing/odd
        ``snapshot_id`` is stamped to the ``death-<ms>`` shape so reads find
        it. Never raises.

        The manifest is caller-supplied (a pod the supervisor doesn't fully
        trust), so it is size-capped before it touches disk — a compromised or
        buggy pod can't fill the supervisor's filesystem with one forensic
        write."""
        ts = float(self._clock() if captured_at is None else captured_at)
        sid = str(manifest.get("snapshot_id") or "")
        if not _FORENSIC_ID_RE.match(sid):
            sid = f"death-{int(ts * 1000)}"
        try:
            if not isinstance(manifest, dict):
                raise ValueError("manifest must be a dict")
            m = dict(manifest)
            m["snapshot_id"] = sid
            m.setdefault("captured_at", ts)
            payload = json.dumps(m, indent=2, sort_keys=True)
            if len(payload.encode("utf-8")) > _EXTERNAL_FORENSIC_MAX_BYTES:
                raise ValueError(
                    f"external forensic manifest exceeds "
                    f"{_EXTERNAL_FORENSIC_MAX_BYTES} bytes"
                )
            self._forensics_root.mkdir(parents=True, exist_ok=True)
            out = self._forensics_root / f"{sid}.json"
            self._atomic_write(out, payload)
            return SomaForensicSnapshot(
                captured=True, reason=str(m.get("reason", "")),
                snapshot_id=sid, path=str(out),
                darwin_id=str(m.get("darwin_id", "")),
                file_count=int(m.get("file_count") or 0),
                total_bytes=int(m.get("total_bytes") or 0),
                captured_at=float(m.get("captured_at") or ts),
                digest=str(m.get("soma_digest", "")),
            )
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            return SomaForensicSnapshot(
                captured=False, reason=str(manifest.get("reason", "")),
                snapshot_id=sid, path=None,
                darwin_id=str(manifest.get("darwin_id", "")),
                file_count=0, total_bytes=0, captured_at=ts, digest="",
                failure=f"{type(exc).__name__}: {exc}",
            )

    def list_forensic_snapshots(self) -> list[dict[str, Any]]:
        """Summaries (metadata only, NO file contents) of every persisted
        death-time snapshot, newest first. Empty when none were captured.
        Cheap for an operator index view — the heavy ``contents`` map is read
        only via :meth:`read_forensic_snapshot`."""
        out: list[dict[str, Any]] = []
        if not self._forensics_root.is_dir():
            return out
        for path in self._forensics_root.glob("death-*.json"):
            try:
                m = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            out.append({
                "snapshot_id": m.get("snapshot_id"),
                "darwin_id": m.get("darwin_id"),
                "reason": m.get("reason"),
                "captured_at": m.get("captured_at"),
                "file_count": m.get("file_count"),
                "total_bytes": m.get("total_bytes"),
                "soma_digest": m.get("soma_digest"),
                "metadata": m.get("metadata", {}),
                "path": str(path),
            })
        out.sort(key=lambda r: r.get("captured_at") or 0.0, reverse=True)
        return out

    def read_forensic_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        """The full persisted manifest (including the ``contents`` file map)
        for one snapshot, or ``None`` when unknown / unreadable. The id is
        validated to the ``death-<ms>`` shape, so it can never traverse
        outside the forensics root."""
        if not _FORENSIC_ID_RE.match(snapshot_id):
            return None
        path = self._forensics_root / f"{snapshot_id}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

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
