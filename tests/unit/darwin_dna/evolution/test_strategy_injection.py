"""Soma strategy injection seam (docs/engineering/33).

The genome binds exactly one prior — ``founding_strategy()`` — unless the
platform has loaded this Darwin's evolved soma and injects the validated
overlay via ``platform.strategy``. That seam is what makes self-modification
actually change behaviour (the platform-side :class:`SomaLoader` builds the
overlay; this proves the genome honours it). With nothing injected, the
founding prior is in effect — the sim/parity guarantee is preserved.
"""

from __future__ import annotations

from darwin_dna.evolution import boot
from darwin_dna.strategies import StrategyAction, StrategySpec, founding_strategy
from darwin_fakes import make_platform


class _OverlayStrategy:
    """A stand-in for a loaded soma strategy (structurally a Strategy)."""

    _spec = StrategySpec(
        id="evolved_v9", archetype="content.affiliate",
        neurons_used=frozenset(), required_capabilities=frozenset(),
        fitness_signals=["x"], est_daily_cost_usdc=0.1,
        revenue_cadence_days=7.0,
    )

    @property
    def spec(self) -> StrategySpec:
        return self._spec

    def propose(self, obs) -> StrategyAction:  # noqa: ARG002
        return StrategyAction(kind="wait", reason="overlay")


def test_default_runtime_uses_founding_strategy(vclock):
    """No injection → the founding prior. The seam must not change defaults
    (the 90-day sim + cross-engine parity depend on it)."""
    platform, _ = make_platform(opening_usdc=10.0, clock=vclock)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    assert rt.strategy.spec.id == founding_strategy().spec.id


def test_runtime_uses_injected_overlay_strategy(vclock):
    """``platform.strategy = X`` → ``rt.strategy is X`` — the supervisor /
    orchestrator inject the SomaLoader's overlay here so a Darwin runs its
    promoted self-modifications."""
    platform, _ = make_platform(opening_usdc=10.0, clock=vclock)
    overlay = _OverlayStrategy()
    platform.strategy = overlay
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    assert rt.strategy is overlay
    assert rt.strategy.spec.id == "evolved_v9"
