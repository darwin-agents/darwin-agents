"""Calibrated simulation scenarios. NOT part of the genome.

Each scenario is a world the genome must survive (or die gracefully in). They
exist so the simulation tests an *invariant*, not a vibe.
"""

from __future__ import annotations

from dataclasses import dataclass

from .world import RevenueModel


@dataclass
class Scenario:
    name: str
    opening_usdc: float
    revenue: RevenueModel
    captcha_every: int
    expectation: str  # "survives" | "reproduces" | "dies_gracefully"


SCENARIOS: dict[str, Scenario] = {
    # The Layer 0 done bar: 90 days, survives, profits, reproduces ≥ once.
    "default": Scenario(
        name="default",
        opening_usdc=80.0,
        revenue=RevenueModel(),
        captcha_every=9,
        expectation="reproduces",
    ),
    # Thin economy + thin runway: the Darwin should die, but *cleanly* —
    # final ledger written, audit chain intact, owner notified (P2/P7).
    "harsh": Scenario(
        name="harsh",
        opening_usdc=1.3,  # thin runway: seeding cost + metabolic burn sinks it
        revenue=RevenueModel(usd_per_effective_piece=0.05),
        captcha_every=3,
        expectation="dies_gracefully",
    ),
    # Captcha-heavy: exercises the escalate-don't-fail path hard (P4).
    "captcha_storm": Scenario(
        name="captcha_storm",
        opening_usdc=120.0,
        revenue=RevenueModel(usd_per_effective_piece=2.2),
        captcha_every=2,
        expectation="survives",
    ),
}
