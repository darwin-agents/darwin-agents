#!/usr/bin/env python3
"""Sync CHANGELOG.md [Unreleased] from git-cliff; cut a semver release.

Usage:
    python scripts/changelog.py sync
    python scripts/changelog.py release 0.0.2 [--no-tag] [--no-bump]
    python scripts/changelog.py preview
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
    block = block.strip() + "\n"
    if not block.endswith("\n\n"):
        block += "\n"
    return block


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


def _normalize_version(raw: str) -> tuple[str, str]:
    """Return (semver, git tag) e.g. ('0.0.2', 'v0.0.2')."""
    ver = raw.strip().removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", ver):
        sys.exit(f"invalid version {raw!r} — expected semver like 0.0.2")
    return ver, f"v{ver}"


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
    section = _run_git_cliff("-u")
    updated = _replace_unreleased(_read(), section)
    _write(updated)
    print(f"Updated {UNRELEASED} in {CHANGELOG.relative_to(ROOT)} from git history.")


def cmd_release(
    version: str,
    *,
    tag: bool,
    bump: bool,
    commit: bool,
    push: bool,
) -> None:
    ver, tag_name = _normalize_version(version)

    if tag and _tag_exists(tag_name):
        sys.exit(f"git tag {tag_name} already exists")

    release_section = _run_git_cliff("-t", tag_name)
    if UNRELEASED in release_section:
        sys.exit("git-cliff returned an [Unreleased] block for -t; check cliff.toml")

    text = _read()
    text = _replace_unreleased(text, _empty_unreleased())
    text = _insert_after_unreleased(text, release_section)
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
    rel.add_argument("version", help="semver (e.g. 0.0.2 or v0.0.2)")
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
