"""Allowed business categories — a whitelist (docs/engineering/02).

Data, not logic. A category outside this set is refused even if the
orchestrator's planner is convinced it's a great idea. Evolution explores
*within* the whitelist; it never argues its way past it.

Trading, prediction/betting markets and DeFi are explicitly in scope
(docs/engineering/05, recent commit) — they are bounded by the treasury hard
caps and the kill switch like any other activity, not by a separate gate.
"""

from __future__ import annotations

ALLOWED_CATEGORIES: frozenset[str] = frozenset(
    {
        "content.blog",  # the founding strategy's home turf
        "content.affiliate",
        "content.newsletter",
        "content.social",
        "digital_products",  # templates, ebooks, courses
        "saas.micro",  # tiny tools / APIs the Darwin builds and sells
        "api_reselling",  # value-add reselling of upstream APIs
        "marketplace_arbitrage",
        "services.automation",  # done-for-you automation gigs
        "advertising.performance",  # running paid acquisition for the above
        "trading.crypto",  # markets — bounded by caps + kill switch only
        "trading.financial",
        "markets.prediction",
        "defi.yield",
        "research.paid",  # paid reports / data products
    }
)


def is_allowed(category: str) -> bool:
    return category in ALLOWED_CATEGORIES
