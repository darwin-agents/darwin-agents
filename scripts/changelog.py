#!/usr/bin/env python3
"""Sync CHANGELOG.md [Unreleased] from git-cliff; cut a semver release.

Prefer the Makefile wrappers (``make changelog-sync``, ``make release``).

Usage:
    python scripts/changelog.py sync
    python scripts/changelog.py preview
    python scripts/changelog.py release 0.0.2 [--no-tag] [--no-bump] [--no-commit] [--no-push]

Release order (default): update CHANGELOG + pyproject → commit → annotated tag → push + --tags.
A hand-written summary paragraph directly under ``## [Unreleased]`` survives ``sync`` and moves
into the version section on ``release``.
Uses ``git-cliff -t`` so only commits since the previous tag are promoted (not full history).
Omit ``version`` to bump the latest release patch (e.g. 0.0.2 → 0.0.3). Explicit versions must be greater.
Commit message: ``chore(release): prepare v{version}``. Only stages CHANGELOG.md and pyproject.toml.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
PYPROJECT = ROOT / "pyproject.toml"
UNRELEASED = "## [Unreleased]"
VERSION_HEADING = re.compile(r"^## \[(?!Unreleased\])")
VERSION_IN_HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\]")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _run_git_cliff(*args: str) -> str:
    try:
        proc = subprocess.run(
            ["git-cliff", *args],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        sys.exit("git-cliff not found (https://git-cliff.org) — install it: `cargo install git-cliff`")
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(exc.stderr or "")
        sys.exit(exc.returncode)
    return proc.stdout.strip() + "\n"


def _read() -> str:
    if not CHANGELOG.is_file():
        sys.exit(f"missing {CHANGELOG}")
    return CHANGELOG.read_text(encoding="utf-8")


def _write(text: str) -> None:
    CHANGELOG.write_text(text, encoding="utf-8")


def _unreleased_bounds(lines: list[str]) -> tuple[int, int]:
    """Line indices [start, end) of the ## [Unreleased] section."""
    start = next((i for i, line in enumerate(lines) if line.strip() == UNRELEASED), None)
    if start is None:
        sys.exit(f"{CHANGELOG.name} has no {UNRELEASED} section")

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if VERSION_HEADING.match(lines[i]):
            end = i
            break
    return start, end


def _normalize_block(block: str) -> str:
    """Format git-cliff output to match Keep a Changelog spacing in CHANGELOG.md."""
    block = re.sub(r"\n{3,}", "\n\n", block.strip())
    lines = block.split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith("### ") and out and out[-1].startswith("- "):
            out.append("")
        out.append(line)
        if line.startswith("### ") and i + 1 < len(lines) and lines[i + 1].startswith("- "):
            out.append("")
    return "\n".join(out) + "\n\n"


def _replace_unreleased(text: str, new_section: str) -> str:
    lines = text.splitlines(keepends=True)
    start, end = _unreleased_bounds(lines)
    return "".join(lines[:start]) + _normalize_block(new_section) + "".join(lines[end:])


def _insert_after_unreleased(text: str, section: str) -> str:
    lines = text.splitlines(keepends=True)
    _, end = _unreleased_bounds(lines)
    block = _normalize_block(section)
    return "".join(lines[:end]) + block + "".join(lines[end:])


def _empty_unreleased() -> str:
    return f"{UNRELEASED}\n"


def _unreleased_preamble(text: str) -> str:
    """Hand-written prose between ``## [Unreleased]`` and its first ``###``
    group. Kept across syncs and carried into the release section, so a
    release summary isn't overwritten by the generated entries."""
    lines = text.splitlines(keepends=True)
    start, end = _unreleased_bounds(lines)
    prose: list[str] = []
    for line in lines[start + 1 : end]:
        if line.startswith("### "):
            break
        prose.append(line)
    return "".join(prose).strip()


_UNRELEASED_LINK = re.compile(r"^\[Unreleased\]: (?P<base>\S+)/compare/(?P<prev>\S+)\.\.\.HEAD$", re.MULTILINE)


def _update_compare_links(text: str, tag: str) -> str:
    """Point ``[Unreleased]`` at ``tag...HEAD`` and add ``[ver]: prev...tag``
    (Keep a Changelog link references). No-op when the file has none."""
    m = _UNRELEASED_LINK.search(text)
    if m is None:
        return text
    base, prev = m.group("base"), m.group("prev")
    ver = tag.removeprefix("v")
    new = f"[Unreleased]: {base}/compare/{tag}...HEAD\n[{ver}]: {base}/compare/{prev}...{tag}"
    return text[: m.start()] + new + text[m.end() :]


def _with_preamble(section: str, preamble: str) -> str:
    """Insert ``preamble`` right after the section's ``## [...]`` heading."""
    if not preamble:
        return section
    heading, _, rest = section.partition("\n")
    return f"{heading}\n\n{preamble}\n\n{rest}"


def _semver_key(ver: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in ver.split("."))


def _parse_semver(raw: str) -> str:
    ver = raw.strip().removeprefix("v")
    if not SEMVER_RE.fullmatch(ver):
        sys.exit(f"invalid version {raw!r} — expected semver like 0.0.2")
    return ver


def _normalize_version(raw: str) -> tuple[str, str]:
    """Return (semver, git tag) e.g. ('0.0.2', 'v0.0.2')."""
    ver = _parse_semver(raw)
    return ver, f"v{ver}"


def _read_pyproject_version() -> str | None:
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    if not match:
        return None
    ver = match.group(1)
    return ver if SEMVER_RE.fullmatch(ver) else None


def _read_changelog_latest_version() -> str | None:
    for line in _read().splitlines():
        match = VERSION_IN_HEADING.match(line)
        if match:
            return match.group(1)
    return None


def _read_latest_git_tag_version() -> str | None:
    proc = subprocess.run(
        ["git", "tag", "-l", "v*", "--sort=-v:refname"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    for line in proc.stdout.splitlines():
        ver = line.strip().removeprefix("v")
        if SEMVER_RE.fullmatch(ver):
            return ver
    return None


def _latest_release_version() -> str | None:
    """Highest semver among git tags, pyproject.toml, and CHANGELOG release headings."""
    candidates: list[str] = []
    for reader in (_read_latest_git_tag_version, _read_pyproject_version, _read_changelog_latest_version):
        ver = reader()
        if ver:
            candidates.append(ver)
    if not candidates:
        return None
    return max(candidates, key=_semver_key)


def _next_patch_version(last: str) -> str:
    major, minor, patch = _semver_key(last)
    return f"{major}.{minor}.{patch + 1}"


def _resolve_release_version(version: str | None) -> tuple[str, str]:
    last = _latest_release_version()
    if version is None:
        ver = _next_patch_version(last) if last else "0.0.1"
        if last:
            print(f"No version given — releasing {ver} (latest was {last})")
        else:
            print(f"No version given — releasing {ver} (first release)")
    else:
        ver, _ = _normalize_version(version)

    if last is not None and _semver_key(ver) <= _semver_key(last):
        sys.exit(f"version {ver} must be greater than latest release {last}")

    return _normalize_version(ver)


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, check=check, text=True)


def _tag_exists(tag: str) -> bool:
    proc = _git("rev-parse", "--verify", f"refs/tags/{tag}", check=False)
    return proc.returncode == 0


def _commit_release(ver: str) -> None:
    _git("add", str(CHANGELOG.relative_to(ROOT)), str(PYPROJECT.relative_to(ROOT)))
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=ROOT,
    )
    if staged.returncode == 0:
        print("No staged changes — skipping commit.")
        return
    message = f"chore(release): prepare v{ver}"
    _git("commit", "-m", message)
    print(f"Committed: {message}")


def _push_release() -> None:
    _git("push")
    _git("push", "--tags")
    print("Pushed branch and tags.")


def _bump_pyproject(version: str) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r'^(version\s*=\s*")[^"]+(")',
        rf'\g<1>{version}\2',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n != 1:
        sys.exit(f"could not bump version in {PYPROJECT.name}")
    PYPROJECT.write_text(new_text, encoding="utf-8")


def cmd_preview() -> None:
    sys.stdout.write(_run_git_cliff("-u"))


def cmd_sync() -> None:
    text = _read()
    section = _with_preamble(_run_git_cliff("-u"), _unreleased_preamble(text))
    updated = _replace_unreleased(text, section)
    _write(updated)
    print(f"Updated {UNRELEASED} in {CHANGELOG.relative_to(ROOT)} from git history.")


def cmd_release(
    version: str | None,
    *,
    tag: bool,
    bump: bool,
    commit: bool,
    push: bool,
) -> None:
    ver, tag_name = _resolve_release_version(version)

    if tag and _tag_exists(tag_name):
        sys.exit(f"git tag {tag_name} already exists")

    last = _latest_release_version()
    if last:
        release_section = _run_git_cliff(f"v{last}..HEAD", "-t", tag_name)
    else:
        release_section = _run_git_cliff("-t", tag_name)

    if UNRELEASED in release_section:
        sys.exit("git-cliff returned an [Unreleased] block; check cliff.toml")
    version_headings = re.findall(r"^## \[", release_section, flags=re.MULTILINE)
    if len(version_headings) != 1:
        sys.exit(
            f"git-cliff release section expected 1 version heading, got {len(version_headings)}; "
            "check cliff.toml render_always"
        )

    text = _read()
    release_section = _with_preamble(release_section, _unreleased_preamble(text))
    text = _replace_unreleased(text, _empty_unreleased())
    text = _insert_after_unreleased(text, release_section)
    text = _update_compare_links(text, tag_name)
    _write(text)

    if bump:
        _bump_pyproject(ver)
        print(f"Bumped {PYPROJECT.name} → {ver}")

    if commit:
        _commit_release(ver)

    if tag:
        _git("tag", "-a", tag_name, "-m", f"Release {tag_name}")
        print(f"Created annotated tag {tag_name}")

    if push:
        if not commit:
            sys.exit("push requires commit (files must be committed before pushing tags)")
        _push_release()

    print(f"Released {ver} in {CHANGELOG.name}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("preview", help="print [Unreleased] to stdout (no file changes)")
    sub.add_parser("sync", help="rewrite [Unreleased] in CHANGELOG.md from commits since last tag")

    rel = sub.add_parser("release", help="promote unreleased commits to a versioned section")
    rel.add_argument(
        "version",
        nargs="?",
        default=None,
        help="semver (e.g. 0.0.2 or v0.0.2); default: patch bump after latest release",
    )
    rel.add_argument("--no-tag", action="store_true", help="do not create a git tag")
    rel.add_argument("--no-bump", action="store_true", help="do not bump pyproject.toml version")
    rel.add_argument("--no-commit", action="store_true", help="do not git commit CHANGELOG + pyproject")
    rel.add_argument("--no-push", action="store_true", help="do not git push branch and tags")

    args = parser.parse_args()
    if args.command == "preview":
        cmd_preview()
    elif args.command == "sync":
        cmd_sync()
    elif args.command == "release":
        cmd_release(
            args.version,
            tag=not args.no_tag,
            bump=not args.no_bump,
            commit=not args.no_commit,
            push=not args.no_push,
        )


if __name__ == "__main__":
    main()
