"""The documentation set is navigable: relative links resolve, every docs
folder has an index, every engineering doc is listed in its index, and doc
numbers are unique (so "doc NN" means one thing). Conventions: docs/README.md."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_LINK = re.compile(r"\]\(([^)\s]+)\)")
_SKIP_DIRS = {".git", ".venv", "node_modules", ".next", ".terraform"}


def _markdown_files(repo: Path) -> list[Path]:
    return sorted(
        p for p in repo.rglob("*.md")
        if not _SKIP_DIRS.intersection(p.relative_to(repo).parts)
    )


def test_relative_links_resolve(repo):
    broken = []
    for md in _markdown_files(repo):
        text = md.read_text(encoding="utf-8", errors="ignore")
        # Fenced code blocks hold example syntax, not links.
        text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        for target in _LINK.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path = target.split("#", 1)[0]
            if path and not (md.parent / path).exists():
                broken.append(f"{md.relative_to(repo)} → {target}")
    assert not broken, "broken relative links:\n" + "\n".join(broken)


@pytest.mark.parametrize("folder", ["overview", "engineering", "frontend", "marketing"])
def test_every_docs_folder_has_an_index(repo, folder):
    assert (repo / "docs" / folder / "00-README.md").is_file()


def test_every_engineering_doc_is_indexed(repo):
    eng = repo / "docs" / "engineering"
    index = (eng / "00-README.md").read_text(encoding="utf-8")
    missing = [
        p.name for p in sorted(eng.glob("*.md"))
        if p.name != "00-README.md" and f"({p.name})" not in index
    ]
    assert not missing, f"not linked from engineering/00-README.md: {missing}"


@pytest.mark.parametrize("folder", ["overview", "engineering", "frontend", "marketing"])
def test_doc_numbers_are_unique(repo, folder):
    seen: dict[str, str] = {}
    for p in sorted((repo / "docs" / folder).glob("[0-9][0-9]-*.md")):
        num = p.name[:2]
        assert num not in seen, f"{folder}/{p.name} reuses number {num} ({seen[num]})"
        seen[num] = p.name
