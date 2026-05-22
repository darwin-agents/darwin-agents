"""content + affiliate — the ONE founding strategy (docs/engineering/18).

Layer 0 ships exactly one strategy on purpose: we want to test that
*evolution finds new strategies*, not that humans pre-loaded them. It is a
clear baseline against which mutations are measured.

The loop: start a topical blog → write content with content.writer → publish
(content.publish_api when it exists; until then browser.operator) → insert
affiliate links → measure (analytics.measure when it exists). The missing
capabilities are declared, not faked, so the orchestrator files real
capability requests (Path B).

The strategy is a *continuously metabolising* organism: a content/affiliate
blog compounds — more indexed pages means more organic traffic means more
affiliate revenue — so the founding strategy keeps writing and publishing
for the whole of the Darwin's life. It seeds an initial library, then grows
it one piece at a time, publishing each as it goes. An earlier Layer 0
returned ``wait`` *forever* once the seed library was up, which left the
organism inert ~100% of its life — a living organism metabolises; an idle
one is just a slow death.
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

# How many pieces to write before the first publish — a blog wants real
# depth at launch. After the seed batch the strategy interleaves
# write/publish so the library keeps growing.
_SEED_PIECES = 5


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
        # Phase 2 — publish every written-but-unpublished piece before
        # writing more, so the library is always live. Each piece is
        # published exactly once: re-publishing the same library would
        # burn cost for nothing, but publishing a *new* piece is growth.
        # Checked before the seed phase so a written seed batch drains to
        # the publisher rather than piling up unpublished.
        if (
            obs.content_pieces >= _SEED_PIECES
            and obs.published_pieces < obs.content_pieces
        ):
            return self._publish_action(obs)

        # Phase 1 — seed: build an initial library before the first
        # publish. A blog wants depth at launch; a thin blog earns
        # nothing.
        if obs.content_pieces < _SEED_PIECES:
            return self._write_action(obs)

        # Phase 1.5 — drain the rest of the seed batch to the publisher.
        if obs.published_pieces < obs.content_pieces:
            return self._publish_action(obs)

        # Phase 3 — steady state: keep growing the library. The blog
        # compounds (more indexed pages → more organic traffic → more
        # affiliate revenue), so the founding strategy never stops
        # publishing. Phase 2 picks the new piece up for publication on
        # the next heartbeat.
        return self._write_action(obs)

    # -- action builders --------------------------------------------------
    def _write_action(self, obs: StrategyObservation) -> StrategyAction:
        topic = _TOPICS[obs.content_pieces % len(_TOPICS)]
        return StrategyAction(
            kind="dispatch",
            neuron="content.writer",
            reason=f"grow the content library (piece {obs.content_pieces + 1})",
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

    def _publish_action(self, obs: StrategyObservation) -> StrategyAction:
        # content.publish_api is a declared-but-unwired capability; until
        # the platform provides it, browser.operator is the publish
        # substrate (Path B). When neither is available, file a real
        # capability request rather than silently dropping the piece.
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
        # content.publish_api exists — dispatch it directly.
        return StrategyAction(
            kind="dispatch",
            neuron="content.publish_api",
            reason=(
                f"publish piece {obs.published_pieces + 1}/"
                f"{obs.content_pieces} via the publish API"
            ),
            task=Task(
                id=f"publish-{obs.published_pieces}",
                kind="web.publish",
                required_tags=frozenset({"web.publish"}),
                category="content.affiliate",
                payload={"url": "https://blog.example/new-post"},
            ),
        )


def founding_strategy() -> Strategy:
    return ContentAffiliateStrategy()
