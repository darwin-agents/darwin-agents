"""LanceDB-backed episodic memory adapter (docs/engineering/03, 11).

Exercises ``LanceDbEpisodicMemory`` against a real embedded LanceDB
(writes to a pytest ``tmp_path``) with an injected deterministic fake
embedder — fully hermetic, no network, bit-for-bit reproducible. The
genome's deterministic :class:`EpisodicMemory` is untouched; this covers
the production substrate the platform injects via
``platform.episodic_memory``.
"""

from __future__ import annotations

import importlib.util

import pytest

from darwin_dna.machinery.memory import EpisodicRecord

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("lancedb") is None,
    reason="requires the [lancedb] extra",
)

# Import after the skip so a missing extra doesn't crash collection.
from darwin_platform.adapters.lancedb_memory import (  # noqa: E402
    LanceDbEpisodicMemory,
)


def _fake_embed(text: str) -> list[float]:
    """Deterministic 16-dim embedding — char-bucket counts, L2-normalised.
    Identical text → identical vector (distance 0); similar text → near."""
    vec = [0.0] * 16
    for ch in text:
        vec[ord(ch) % 16] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def _mem(tmp_path) -> LanceDbEpisodicMemory:
    return LanceDbEpisodicMemory(embed=_fake_embed, db_path=str(tmp_path))


def test_empty_recall_returns_empty_list(tmp_path):
    m = _mem(tmp_path)
    assert m.recall("anything") == []
    assert m.all() == []
    assert len(m) == 0


def test_record_then_recall_returns_episodic_record(tmp_path):
    m = _mem(tmp_path)
    m.record(
        tick=7,
        signature="content.publisher|publish|seo",
        neuron="content.publisher",
        outcome="ok",
        ts=1234.5,
        detail={"k": "v"},
    )
    assert len(m) == 1
    [hit] = m.recall("content.publisher|publish|seo", limit=1)
    assert isinstance(hit, EpisodicRecord)
    assert hit.tick == 7
    assert hit.signature == "content.publisher|publish|seo"
    assert hit.neuron == "content.publisher"
    assert hit.outcome == "ok"
    assert hit.ts == pytest.approx(1234.5)
    assert hit.detail == {"k": "v"}


def test_exact_signature_ranks_first(tmp_path):
    """An identical signature embeds to the same vector — distance 0 — so
    an exact prior ranks above token-overlap neighbours, for free."""
    m = _mem(tmp_path)
    m.record(tick=1, signature="x|publish|alpha", neuron="x", outcome="ok")
    m.record(tick=2, signature="x|publish|beta", neuron="x", outcome="failed")
    m.record(tick=3, signature="y|other|gamma", neuron="y", outcome="ok")
    hits = m.recall("x|publish|alpha", limit=3)
    assert hits[0].signature == "x|publish|alpha"


def test_recall_limit_caps_results(tmp_path):
    m = _mem(tmp_path)
    for i in range(5):
        m.record(tick=i, signature=f"n|k|{i}", neuron="n", outcome="ok")
    assert len(m.recall("n|k|0", limit=3)) == 3
    assert m.recall("n|k|0", limit=0) == []


def test_all_returns_every_record_without_vector_column(tmp_path):
    m = _mem(tmp_path)
    m.record(tick=1, signature="a", neuron="n", outcome="ok")
    m.record(
        tick=2, signature="b", neuron="n",
        outcome="failed", error_code="rate_limited",
    )
    rows = m.all()
    assert {r.signature for r in rows} == {"a", "b"}
    assert all(isinstance(r, EpisodicRecord) for r in rows)
    by_sig = {r.signature: r for r in rows}
    assert by_sig["b"].error_code == "rate_limited"


def test_persists_across_instances(tmp_path):
    """LanceDB is on disk — a fresh adapter at the same path reopens the
    existing table. The supervisor uses this when it rebuilds a per-Darwin
    platform on resume."""
    first = _mem(tmp_path)
    first.record(tick=42, signature="persists", neuron="n", outcome="ok")

    second = LanceDbEpisodicMemory(embed=_fake_embed, db_path=str(tmp_path))
    assert len(second) == 1
    [hit] = second.recall("persists", limit=1)
    assert hit.tick == 42


def test_detail_round_trips_through_json(tmp_path):
    m = _mem(tmp_path)
    payload = {"counts": [1, 2, 3], "nested": {"flag": True}}
    m.record(tick=1, signature="s", neuron="n", outcome="ok", detail=payload)
    [hit] = m.recall("s", limit=1)
    assert hit.detail == payload


def test_record_with_no_detail_yields_empty_dict(tmp_path):
    m = _mem(tmp_path)
    m.record(tick=1, signature="s", neuron="n", outcome="ok")
    [hit] = m.recall("s", limit=1)
    assert hit.detail == {}
    assert hit.error_code is None


def test_requires_embed_callable(tmp_path):
    with pytest.raises(ValueError, match="embed"):
        LanceDbEpisodicMemory(embed=None, db_path=str(tmp_path))  # type: ignore[arg-type]


def test_requires_non_empty_db_path():
    with pytest.raises(ValueError, match="db_path"):
        LanceDbEpisodicMemory(embed=_fake_embed, db_path="")
