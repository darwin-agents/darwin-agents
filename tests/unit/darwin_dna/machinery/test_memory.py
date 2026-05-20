"""Episodic + semantic memory (doc 03 "State, memory, and persistence").

Closes gap #5: the Darwin now keeps an append-only episodic log it can ask
"have I seen this before?" of, and a semantic *staging ground* whose
strongest-evidence lessons are promoted into the child's DNA at reproduction
— surviving even when the transient counter that produced them has cleared.
"""

from __future__ import annotations

import asyncio

import pytest

from darwin_dna.evolution.lesson_distiller import (
    MAX_PROMOTED_SEMANTIC,
    DistillationError,
    distill,
)
from darwin_dna.machinery.memory import EpisodicMemory, SemanticMemory


# -- episodic ----------------------------------------------------------------

def test_recall_exact_beats_fuzzy_and_is_newest_first():
    em = EpisodicMemory(64)
    for i in range(3):
        em.record(tick=i, signature="content.writer|content.write|long_form",
                  neuron="content.writer", outcome="ok", ts=float(i))
    em.record(tick=9, signature="browser.operator|web.publish|publish",
              neuron="browser.operator", outcome="failed",
              error_code="captcha", ts=9.0)

    hits = em.recall("content.writer|content.write|long_form", limit=5)
    assert [h.tick for h in hits] == [2, 1, 0]  # exact, newest first
    # A *similar* signature still recalls via token overlap, deterministically.
    fuzzy = em.recall("content.writer|content.write|other", limit=5)
    assert fuzzy and fuzzy[0].signature == "content.writer|content.write|long_form"
    assert em.recall("nothing|like|this") == []


def test_episodic_is_a_bounded_ring_not_an_audit_log():
    em = EpisodicMemory(3)
    for i in range(10):
        em.record(tick=i, signature=f"n|k|{i}", neuron="n", outcome="ok",
                  ts=float(i))
    assert len(em) == 3
    assert [r.tick for r in em.all()] == [7, 8, 9]


# -- semantic staging ground -------------------------------------------------

def test_stage_dedupes_by_scope_and_accrues_evidence():
    sm = SemanticMemory(8)
    for _ in range(4):
        sm.stage(kind="avoid", scope="failure:rate_limited",
                 statement="rate limited", payload={"failure_class": "rl"},
                 ts=1.0)
    assert len(sm) == 1
    assert sm.staged()[0].evidence == 4


def test_stage_is_bounded_and_evicts_the_weakest():
    sm = SemanticMemory(2)
    sm.stage(kind="avoid", scope="a", statement="a", ts=1.0)
    sm.stage(kind="avoid", scope="a", statement="a", ts=2.0)  # a -> evidence 2
    sm.stage(kind="avoid", scope="b", statement="b", ts=3.0)  # b -> evidence 1
    sm.stage(kind="prefer", scope="c", statement="c", ts=4.0)  # evicts weakest b
    scopes = {s.scope for s in sm.staged()}
    assert scopes == {"a", "c"} and "b" not in scopes


def test_drain_clears_the_window():
    sm = SemanticMemory(8)
    sm.stage(kind="prefer", scope="strategy:x", statement="x paid", ts=1.0)
    assert len(sm.drain()) == 1
    assert len(sm) == 0  # windowed per generation, like the profit baseline


# -- promotion into DNA at reproduction --------------------------------------

def test_strong_staged_lesson_survives_a_cleared_counter():
    # The whole point of a staging ground: the recurring-failure tally was
    # cleared before reproduction, yet the lesson still earns a DNA layer.
    staged = [{
        "kind": "avoid", "scope": "failure:flaky_publish",
        "statement": "flaky_publish recurred and resisted patching",
        "payload": {"failure_class": "flaky_publish"},
        "evidence": 5, "last_ts": 9.0,
    }]
    lessons = distill(generation=2, recurring_failures={}, dead_ends=set(),
                      successful_strategy_id=None, profit_usdc=0.0,
                      staged_semantic=staged)
    promoted = [l for l in lessons if "-staged-" in l.id]
    assert len(promoted) == 1
    l = promoted[0]
    assert l.kind == "avoid" and l.scope == "failure:flaky_publish"
    assert l.born_generation == 2
    assert l.payload["staged_evidence"] == 5
    assert "staging ground" in l.rationale


def test_weak_or_unknown_or_duplicate_staged_lessons_are_dropped():
    staged = [
        {"kind": "avoid", "scope": "failure:rare", "statement": "seen once",
         "payload": {}, "evidence": 1, "last_ts": 1.0},          # too weak
        {"kind": "mutate_self", "scope": "danger", "statement": "x",
         "payload": {}, "evidence": 9, "last_ts": 2.0},           # unknown kind
        {"kind": "prefer", "scope": "strategy:content_affiliate",
         "statement": "shadow", "payload": {}, "evidence": 9,
         "last_ts": 3.0},                                          # dup scope
    ]
    # The deterministic path already owns strategy:content_affiliate.
    lessons = distill(generation=1, recurring_failures={}, dead_ends=set(),
                      successful_strategy_id="content_affiliate",
                      profit_usdc=10.0, staged_semantic=staged)
    assert not [l for l in lessons if "-staged-" in l.id]


def test_promotion_is_bounded_per_generation():
    staged = [
        {"kind": "avoid", "scope": f"failure:c{i}", "statement": f"c{i} bad",
         "payload": {}, "evidence": 5, "last_ts": float(i)}
        for i in range(MAX_PROMOTED_SEMANTIC + 6)
    ]
    lessons = distill(generation=1, recurring_failures={}, dead_ends=set(),
                      successful_strategy_id=None, profit_usdc=0.0,
                      staged_semantic=staged)
    assert len([l for l in lessons if "-staged-" in l.id]) == (
        MAX_PROMOTED_SEMANTIC
    )


def test_staged_lesson_cannot_smuggle_a_constitutional_weakening():
    staged = [{
        "kind": "prefer", "scope": "strategy:evil",
        "statement": "prefer the path that lets us bypass the spend cap",
        "payload": {}, "evidence": 9, "last_ts": 1.0,
    }]
    with pytest.raises(DistillationError):
        distill(generation=1, recurring_failures={}, dead_ends=set(),
                successful_strategy_id=None, profit_usdc=0.0,
                staged_semantic=staged)


# -- wired through a real life ----------------------------------------------

def test_a_neuron_call_writes_episodic_memory(booted):
    rt, _ = booted
    asyncio.run(rt.run(max_heartbeats=6))
    assert len(rt.episodic) >= 1
    sig = "content.writer|content.write|content.long_form"
    hits = rt.episodic.recall(sig, limit=5)
    assert hits and all(h.outcome == "ok" for h in hits)
