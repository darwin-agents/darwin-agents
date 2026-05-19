"""Orchestrator engine parity (docs/engineering/12, 18).

The node functions are framework-agnostic; the internal runner and a real
LangGraph wiring must produce the *same* decisions, or "boots on first run"
offline is a different Darwin than the one that runs in production.
"""

from __future__ import annotations

import asyncio

import pytest

from darwin_dna.evolution import boot
from darwin_fakes import make_platform


def _run(engine: str, beats: int):
    box = {"n": 1_700_000_000.0}
    clk = lambda: box["n"]  # noqa: E731
    slp = lambda s: box.__setitem__("n", box["n"] + s)  # noqa: E731
    platform, stack = make_platform(opening_usdc=80.0, clock=clk)
    rt = boot(platform, clock=clk, sleep=slp, engine=engine)
    asyncio.run(rt.run(max_heartbeats=beats))
    return rt, stack


def test_internal_and_langgraph_reach_the_same_state():
    pytest.importorskip("langgraph")
    a, sa = _run("internal", 12)
    b, sb = _run("langgraph", 12)

    for field in ("tick", "dispatches_done", "successful_dispatches",
                  "content_pieces", "published_pieces", "alive"):
        assert getattr(a.state, field) == getattr(b.state, field), field

    # Both engines checkpoint at every node boundary and keep an unbroken
    # audit chain.
    assert sa.checkpoint.saves > 0 and sb.checkpoint.saves > 0
    a.audit.verify()
    b.audit.verify()


def test_langgraph_runner_actually_used():
    pytest.importorskip("langgraph")
    rt, _ = _run("langgraph", 1)
    assert type(rt._graph).__name__ == "_LGRunner"
