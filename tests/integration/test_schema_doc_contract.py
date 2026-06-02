"""Contract test binding the platform schema to its reference doc (doc 23).

This is the safeguard whose ABSENCE let doc 23 drift to v2 while the code moved
to v11 with 9 undocumented money/ops tables. Every table the schema creates
must be documented in 23-data-model-reference.md, and the documented version
must match the code — or this test fails.
"""

from __future__ import annotations

from pathlib import Path

from darwin_platform.runtime.persistence import (
    CURRENT_SCHEMA_VERSION,
    PersistenceStore,
)

_DOC = (
    Path(__file__).resolve().parents[2]
    / "docs" / "engineering" / "23-data-model-reference.md"
)


def _actual_tables(tmp_path) -> set[str]:
    store = PersistenceStore(tmp_path / "platform.db")
    rows = store._backend.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows if not str(r[0]).startswith("sqlite_")}


def test_every_schema_table_is_documented(tmp_path):
    doc = _DOC.read_text()
    undocumented = sorted(t for t in _actual_tables(tmp_path) if t not in doc)
    assert not undocumented, (
        f"tables present in the schema but missing from doc 23: {undocumented}. "
        "Document them in docs/engineering/23-data-model-reference.md."
    )


def test_schema_version_matches_doc(tmp_path):
    store = PersistenceStore(tmp_path / "platform.db")
    assert store.schema_version() == CURRENT_SCHEMA_VERSION
    doc = _DOC.read_text()
    assert f"**{CURRENT_SCHEMA_VERSION}**" in doc, (
        f"doc 23 must state schema version {CURRENT_SCHEMA_VERSION}"
    )
