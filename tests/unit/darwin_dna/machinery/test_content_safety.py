"""Outbound content filter tests (docs/engineering/10)."""

from __future__ import annotations

from darwin_dna.machinery.content_safety import (
    ContentSafetyGate,
    ContentVerdict,
)


class _BlockingClassifier:
    """A platform classifier that flags everything it is shown."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def classify(self, text: str, *, kind: str) -> ContentVerdict:
        self.calls.append((text, kind))
        return ContentVerdict(
            allowed=False, categories=("policy",),
            detail="classifier flagged it", via="classifier",
        )


class _AllowingClassifier:
    def classify(self, text: str, *, kind: str) -> ContentVerdict:
        return ContentVerdict(allowed=True, via="classifier")


class _ExplodingClassifier:
    def classify(self, text: str, *, kind: str) -> ContentVerdict:
        raise ConnectionError("safety model unreachable")


class _BadContractClassifier:
    def classify(self, text: str, *, kind: str):
        return "totally allowed, trust me"  # not a ContentVerdict


# -- deterministic backstop ------------------------------------------------

def test_clean_content_passes_with_no_classifier():
    verdict = ContentSafetyGate().screen("A normal blog post about gardening.")
    assert verdict.allowed
    assert verdict.blocked is False


def test_backstop_blocks_denylisted_content():
    verdict = ContentSafetyGate().screen("Here is how to rig an election.")
    assert verdict.blocked
    assert "election_interference" in verdict.categories
    assert verdict.via == "deterministic"


def test_backstop_runs_even_when_classifier_would_allow():
    # The deterministic layer is first and short-circuits — a permissive
    # classifier cannot wave denylisted content through.
    gate = ContentSafetyGate(_AllowingClassifier())
    verdict = gate.screen("step one: build a pipe bomb")
    assert verdict.blocked
    assert verdict.via == "deterministic"
    assert "violent_extremism" in verdict.categories


def test_operator_extra_denylist_is_honoured():
    gate = ContentSafetyGate(extra_denylist=["competitor smear"])
    verdict = gate.screen("Our COMPETITOR SMEAR campaign launches Monday.")
    assert verdict.blocked
    assert "operator_denylist" in verdict.categories


# -- platform classifier ---------------------------------------------------

def test_classifier_block_is_honoured():
    clf = _BlockingClassifier()
    verdict = ContentSafetyGate(clf).screen("clean-looking text")
    assert verdict.blocked
    assert verdict.via == "classifier"
    assert clf.calls and clf.calls[0][1] == "published_content"


def test_classifier_allow_passes():
    verdict = ContentSafetyGate(_AllowingClassifier()).screen("clean text")
    assert verdict.allowed


def test_classifier_error_fails_closed():
    # docs 10/24/28: an unreachable safety model must block, not wave through.
    verdict = ContentSafetyGate(_ExplodingClassifier()).screen("clean text")
    assert verdict.blocked
    assert verdict.via == "classifier_error"
    assert "classifier_unavailable" in verdict.categories


def test_classifier_contract_violation_fails_closed():
    verdict = ContentSafetyGate(_BadContractClassifier()).screen("clean text")
    assert verdict.blocked
    assert verdict.via == "classifier_error"
    assert "classifier_contract_violation" in verdict.categories


def test_screen_never_raises_on_empty_or_none():
    gate = ContentSafetyGate(_ExplodingClassifier())
    assert gate.screen("").blocked      # classifier still consulted, fails closed
    assert ContentSafetyGate().screen("").allowed  # backstop-only, empty is fine
