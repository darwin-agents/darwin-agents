"""Episodic memory injection seam (docs/engineering/03).

The genome ships a deterministic :class:`EpisodicMemory` (the 90-day sim
and cross-engine parity depend on it). When the platform supplies
``platform.episodic_memory``, :class:`DarwinRuntime` uses that injected
substrate in place of the default — the seam through which
``LanceDbEpisodicMemory`` (and any future production memory) enters
without touching the DNA. With nothing injected, the deterministic
default is in effect.
"""

from __future__ import annotations

from darwin_dna.evolution import boot
from darwin_dna.machinery.memory import EpisodicMemory
from darwin_fakes import make_platform


class _StubEpisodicMemory:
    """Records calls so a test can prove the runtime used *this* instance,
    not a freshly constructed deterministic default."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def record(self, **kw) -> None:
        self.records.append(kw)

    def recall(self, signature, *, limit=5):  # noqa: ARG002
        return []

    def __len__(self) -> int:
        return len(self.records)

    def all(self):
        return list(self.records)


def test_default_runtime_uses_deterministic_episodic_memory(vclock):
    """No injection → genome falls back to its in-process EpisodicMemory.
    This is the sim/parity guarantee — the seam must not change defaults."""
    platform, _ = make_platform(opening_usdc=10.0, clock=vclock)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    assert isinstance(rt.episodic, EpisodicMemory)


def test_runtime_uses_injected_episodic_memory(vclock):
    """``stack.episodic_memory = X`` → ``rt.episodic is X``. This is what
    lets the supervisor swap in ``LanceDbEpisodicMemory`` per-Darwin
    without touching the genome boot path."""
    _, stack = make_platform(opening_usdc=10.0, clock=vclock)
    injected = _StubEpisodicMemory()
    stack.episodic_memory = injected
    platform = stack.platform()  # rebuild the Platform with the injection
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    assert rt.episodic is injected
