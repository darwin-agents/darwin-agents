"""Outbound content filter (docs/engineering/10 — "Outbound content filter").

Content a Darwin's neurons are about to publish is screened here *before* it
leaves the organism. Flagged content blocks publication and escalates to the
owner. The Darwin cannot reason its way past a block, the same way it cannot
for the kill switch or the treasury policy — the gate sits in the publishing
neuron's ``execute`` path, which is DNA and out of soma's patch scope.

Two layers, defense in depth:

1. **The platform safety classifier.** A real text/vision safety model wired
   in by the platform through ``NeuronContext.content_classifier``. Free-text
   judgement deliberately lives *outside* the genome: the genome must not be
   something you can argue out of its own safety rails, so it only ever
   *honours* a verdict — it never produces one by reasoning.
2. **A deterministic genome backstop.** A short, explicit denylist of the
   unambiguously forbidden. It runs even when no classifier is configured, so
   no publish path is ever completely unscreened. It is intentionally crude
   and high-precision — there to catch the worst, not to be a good classifier.

Fails closed: if a classifier is wired but errors, the content is treated as
flagged. Better to block a healthy post than to publish an unscreened one.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ContentVerdict:
    """The outcome of screening one piece of outbound content."""

    allowed: bool
    categories: tuple[str, ...] = ()
    detail: str = ""
    # "deterministic" | "classifier" | "classifier_error"
    via: str = "deterministic"

    @property
    def blocked(self) -> bool:
        return not self.allowed


_ALLOWED = ContentVerdict(allowed=True, detail="no rule flagged the content")


@runtime_checkable
class ContentClassifier(Protocol):
    """Platform-supplied text/vision safety model, injected per invocation.

    ``classify`` returns a :class:`ContentVerdict`. Raising is allowed — the
    gate treats any failure as a block (fail closed)."""

    def classify(self, text: str, *, kind: str) -> ContentVerdict: ...


# Deterministic backstop. Short and high-precision on purpose — the platform
# classifier is the real filter; this only guarantees nothing publishes
# completely unscreened. Phrase (lowercased substring) -> forbidden category.
_BACKSTOP_DENYLIST: dict[str, str] = {
    "child sexual abuse": "csam",
    "child porn": "csam",
    "build a pipe bomb": "violent_extremism",
    "make a pipe bomb": "violent_extremism",
    "join the jihad": "violent_extremism",
    "how to rig an election": "election_interference",
    "how to rig the election": "election_interference",
    "cast a fraudulent ballot": "election_interference",
    "enter your seed phrase": "credential_phishing",
    "send your private key": "credential_phishing",
    "verify your wallet by sending": "credential_phishing",
}


class KeywordContentClassifier:
    """A concrete :class:`ContentClassifier` the platform can wire into the
    injection seam (Supervisor ``content_classifier``) without an LLM.

    It is deliberately more than the gate's tiny hard-coded backstop: operators
    pass a ``categories`` map of ``category -> phrases`` tuned to their policy
    and risk surface (and can update it without a redeploy). Production
    deployers may instead inject an LLM-backed classifier with the same
    ``classify(text, *, kind) -> ContentVerdict`` surface; this gives a real,
    testable default so the seam isn't a no-op (doc 10)."""

    def __init__(self, categories: dict[str, Iterable[str]] | None = None):
        self._phrases: dict[str, str] = {}
        for category, phrases in (categories or {}).items():
            for p in phrases:
                cleaned = str(p).strip().lower()
                if cleaned:
                    self._phrases[cleaned] = category

    def classify(self, text: str, *, kind: str) -> ContentVerdict:
        hay = (text or "").lower()
        hits = sorted({cat for phrase, cat in self._phrases.items()
                       if phrase in hay})
        if hits:
            return ContentVerdict(
                allowed=False,
                categories=tuple(hits),
                detail=f"keyword classifier flagged: {', '.join(hits)}",
                via="classifier",
            )
        return ContentVerdict(
            allowed=True, detail="keyword classifier: no match", via="classifier",
        )


class ContentSafetyGate:
    """Screens outbound content. Construct per invocation with whatever
    classifier the neuron was handed (or ``None``)."""

    def __init__(
        self,
        classifier: ContentClassifier | None = None,
        *,
        extra_denylist: Iterable[str] = (),
    ):
        self._classifier = classifier
        self._denylist = dict(_BACKSTOP_DENYLIST)
        for phrase in extra_denylist:
            cleaned = phrase.strip().lower()
            if cleaned:
                self._denylist[cleaned] = "operator_denylist"

    def screen(
        self, text: str, *, kind: str = "published_content"
    ) -> ContentVerdict:
        """Screen one piece of content. Returns a verdict; never raises."""
        # 1. Deterministic backstop — always runs, first.
        hay = (text or "").lower()
        hits = sorted({cat for phrase, cat in self._denylist.items()
                       if phrase in hay})
        if hits:
            return ContentVerdict(
                allowed=False,
                categories=tuple(hits),
                detail=f"deterministic backstop flagged: {', '.join(hits)}",
                via="deterministic",
            )
        # 2. Platform classifier — when wired. Fail closed on any failure.
        if self._classifier is not None:
            try:
                verdict = self._classifier.classify(text or "", kind=kind)
            except Exception as exc:  # noqa: BLE001 - fail closed on ANY error
                return ContentVerdict(
                    allowed=False,
                    categories=("classifier_unavailable",),
                    detail=(
                        f"content classifier errored "
                        f"({type(exc).__name__}: {exc})"
                    ),
                    via="classifier_error",
                )
            if not isinstance(verdict, ContentVerdict):
                return ContentVerdict(
                    allowed=False,
                    categories=("classifier_contract_violation",),
                    detail="content classifier returned a non-verdict",
                    via="classifier_error",
                )
            if verdict.blocked:
                return verdict
        return _ALLOWED
