"""content + affiliate — the ONE founding strategy (docs/engineering/18).

Layer 0 ships exactly one strategy on purpose: we want to test that
*evolution finds new strategies*, not that humans pre-loaded them. It is a
clear baseline against which mutations are measured.

The loop: start a topical blog → write content with content.writer → publish
(content.publish_api when it exists; until then browser.operator) → insert
affiliate links → measure (analytics.measure when it exists). The missing
capabilities are declared, not faked, so the orchestrator files real
capability requests (Path B).
"""

from __future__ import annotations

from ..neurons import Task
from ._base import Strategy, StrategyAction, StrategyObservation, StrategySpec

_TOPICS = (
    "budget home espresso",
    "indoor herb gardening",
    "mechanical keyboards",
    "trail running shoes",
    "standing desk ergonomics",
)


class ContentAffiliateStrategy:
    """Implements the :class:`Strategy` protocol structurally."""

    _spec = StrategySpec(
        id="content_affiliate",
        archetype="content.affiliate",
        neurons_used=frozenset({"content.writer", "browser.operator"}),
        required_capabilities=frozenset(
            {"content.publish_api", "analytics.measure"}
        ),
        fitness_signals=["affiliate_revenue_usdc", "published_pieces", "llm_cost"],
        est_daily_cost_usdc=0.50,
        revenue_cadence_days=7.0,  # affiliate commissions post weekly-ish
    )

    @property
    def spec(self) -> StrategySpec:
        return self._spec

    def propose(self, obs: StrategyObservation) -> StrategyAction:
        # Earn-before-everything: the founding strategy is cheap and additive.
        # Build a content library first; a thin blog earns nothing.
        if obs.content_pieces < 5:
            topic = _TOPICS[obs.content_pieces % len(_TOPICS)]
            return StrategyAction(
                kind="dispatch",
                neuron="content.writer",
                reason=f"library is thin ({obs.content_pieces} pieces); write more",
                task=Task(
                    id=f"write-{obs.content_pieces}",
                    kind="content.write",
                    required_tags=frozenset({"content.long_form"}),
                    category="content.affiliate",
                    payload={
                        "topic": topic,
                        "target_words": 900,
                        "affiliate": True,
                    },
                ),
            )

        # Each written piece is published exactly once. Re-publishing the
        # same library every heartbeat would burn browser cost for no revenue
        # — the founding strategy is frugal by construction.
        if obs.published_pieces < obs.content_pieces:
            if "content.publish_api" not in obs.available_neurons:
                if "browser.operator" in obs.available_neurons:
                    return StrategyAction(
                        kind="dispatch",
                        neuron="browser.operator",
                        reason=(
                            f"publish piece {obs.published_pieces + 1}/"
                            f"{obs.content_pieces} via browser substrate"
                        ),
                        task=Task(
                            id=f"publish-{obs.published_pieces}",
                            kind="web.publish",
                            required_tags=frozenset({"web.publish"}),
                            category="content.affiliate",
                            payload={"url": "https://blog.example/new-post"},
                        ),
                    )
                return StrategyAction(
                    kind="capability_request",
                    requested_capability="content.publish_api",
                    reason="recurring need to publish; no neuron can",
                )

        # Library written and fully published: let revenue accrue. Don't burn
        # tokens spinning (docs/engineering/18 failure mode: burns LLM money).
        return StrategyAction(
            kind="wait",
            reason="content library seeded and published; await affiliate revenue",
        )


def founding_strategy() -> Strategy:
    return ContentAffiliateStrategy()
