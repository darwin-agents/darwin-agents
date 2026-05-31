"""The simulated world (docs/engineering/18, "testing strategy").

NOT part of the genome. Mocks the external economy: a virtual clock the
Darwin's heartbeat sleep advances, and an affiliate network that pays bursty
commissions sized by how much publishable content the Darwin has built and how
good it is. Calibrated so that a Darwin that does the work becomes profitable
and can reproduce, while a Darwin that stalls runs out of runway and dies —
which is the whole point of selection (P2).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RevenueModel:
    """Affiliate economics. Weekly-ish commissions that *compound* with the
    published library: a content/affiliate blog's organic traffic grows with
    the number of indexed pages — sub-linearly (each additional page adds less
    than the last, as SEO returns diminish per-page) but without ever
    plateauing (a bigger library always earns more). This is the founding
    strategy's premise — it keeps writing and publishing for life because it
    pays to keep metabolising — so the world must reward a growing library,
    not cap it. Scaled by content quality.

    (An earlier model was concave-to-an-asymptote — ``n/(1+n/k)`` — which
    suited the original "seed 5 pieces then idle" strategy. Once the strategy
    was changed to metabolise continuously, that asymptote made endless
    publishing a pure cost with no upside, so the Darwin bled out and never
    reproduced. The curve below restores the "Layer 0 done" bar.)"""

    cadence_days: float = 7.0
    usd_per_effective_piece: float = 0.12
    quality: float = 0.72  # matches the reasoning-tier writer in the fake LLM
    growth_exponent: float = 0.6  # SEO compounding: revenue ∝ pages**exponent
    diminishing_k: float = 8.0  # launch-ramp softener (small-library knee)

    def commission(self, content_pieces: int) -> float:
        if content_pieces <= 0:
            return 0.0
        # Unbounded but sub-linear in library size: ≈ linear while the blog
        # is tiny (the diminishing_k knee), tending to ∝ n**growth_exponent
        # as it grows. A 1000-post blog out-earns a 100-post one, but not 10x.
        effective = self.diminishing_k * (
            (1.0 + content_pieces / self.diminishing_k) ** self.growth_exponent
            - 1.0
        )
        return round(self.usd_per_effective_piece * effective * self.quality, 4)


class SimWorld:
    """Owns the virtual clock. ``sleep`` is wired in as the Darwin's
    heartbeat sleep, so simulated time only moves when the organism rests —
    deterministic and reproducible."""

    def __init__(
        self,
        *,
        start_ts: float,
        revenue: RevenueModel,
        compute_usd_per_day: float = 0.02,
    ):
        self._now = float(start_ts)
        self._rev = revenue
        self._compute_rate = compute_usd_per_day / 86_400.0
        self._wallet = None
        self._content = lambda: 0
        self._next_payout = float(start_ts) + revenue.cadence_days * 86_400.0
        self.revenue_events: list[tuple[float, float]] = []  # (ts, amount)

    # clock + sleep injected into make_platform / boot
    def clock(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += float(seconds)
        self._charge_compute(float(seconds))
        self._settle_revenue()

    def _charge_compute(self, seconds: float) -> None:
        """The platform's metered compute/hosting bill. A Darwin metabolizes
        capital just by being alive (docs/engineering/05) — so even a perfectly
        idle organism has a clock running against its runway. Guarded: we
        never push the balance negative; the genome's death trigger owns that."""
        if self._wallet is None:
            return
        cost = self._compute_rate * seconds
        bal = self._wallet.balance_usdc()
        if cost > 0 and bal > 0:
            self._wallet.debit(min(cost, bal), "platform:compute")

    def bind(self, *, wallet, content_getter) -> None:
        """Attach the booted Darwin's wallet and a callable returning its
        current publishable-content count."""
        self._wallet = wallet
        self._content = content_getter

    def _settle_revenue(self) -> None:
        # Pay every cadence boundary we have crossed (a long sleep may cross
        # several). Revenue is bursty, exactly as the death-grace window in
        # the genome anticipates (docs/engineering/05).
        while self._wallet is not None and self._now >= self._next_payout:
            amount = self._rev.commission(int(self._content()))
            if amount > 0:
                self._wallet.credit(amount, "affiliate_network")
                self.revenue_events.append((self._next_payout, amount))
            self._next_payout += self._rev.cadence_days * 86_400.0
