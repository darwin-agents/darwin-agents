"""Content-safety classifier injection seam (doc 10).

The genome reads ``context.content_classifier`` but the platform never set it,
so publishing neurons fell back to the tiny deterministic backstop. These tests
pin the new concrete classifier + that a Supervisor-injected classifier reaches
a booted Darwin's platform (and thus its publishing-neuron gate).
"""

from __future__ import annotations

import pytest

from darwin_dna.machinery.content_safety import (
    ContentSafetyGate,
    KeywordContentClassifier,
)
from darwin_platform.runtime.supervisor import Supervisor


# ---------------------------------------------------------------------------
# KeywordContentClassifier + gate integration
# ---------------------------------------------------------------------------

def test_keyword_classifier_flags_configured_phrase():
    clf = KeywordContentClassifier({"scam": ["guaranteed 1000x returns"]})
    v = clf.classify("buy now for guaranteed 1000x returns!", kind="ad")
    assert v.blocked
    assert "scam" in v.categories


def test_keyword_classifier_allows_clean_text():
    clf = KeywordContentClassifier({"scam": ["guaranteed 1000x returns"]})
    assert clf.classify("a normal blog post", kind="blog").allowed


def test_gate_uses_injected_classifier():
    clf = KeywordContentClassifier({"spam": ["click here to win"]})
    gate = ContentSafetyGate(clf)
    # Backstop wouldn't catch this; the injected classifier does.
    assert gate.screen("CLICK HERE TO WIN a prize").blocked
    assert gate.screen("a perfectly normal sentence").allowed


# ---------------------------------------------------------------------------
# Supervisor injection seam
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_supervisor_injects_classifier_into_darwin_platform():
    clf = KeywordContentClassifier({"scam": ["wire me your seed phrase"]})
    sup = Supervisor(heartbeat_seconds=0.001, content_classifier=clf)
    try:
        status = await sup.spawn(opening_usdc=50.0, actor="owner")
        runtime = sup._darwins[status.darwin_id].runtime
        # The classifier reached the Darwin's platform — the loop injects it
        # into each publishing neuron's NeuronContext from here.
        assert runtime._p.content_classifier is clf
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_no_classifier_leaves_platform_unset():
    sup = Supervisor(heartbeat_seconds=0.001)
    try:
        status = await sup.spawn(opening_usdc=50.0, actor="owner")
        runtime = sup._darwins[status.darwin_id].runtime
        assert getattr(runtime._p, "content_classifier", None) is None
    finally:
        await sup.shutdown(kill_running=True)
