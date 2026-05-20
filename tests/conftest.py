"""Shared fixtures. Tests assume an adversarial soma on the hot paths."""

from __future__ import annotations

import pytest

from darwin_fakes import make_platform
from darwin_fakes.fake_platform import SigningService, mint_genesis


class VClock:
    """A controllable virtual clock so every test is deterministic."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


@pytest.fixture
def vclock() -> VClock:
    return VClock()


@pytest.fixture
def signer() -> SigningService:
    return SigningService()


@pytest.fixture
def genesis(signer):
    """A freshly minted, validly signed generation-0 DNA bundle."""
    manifest, blobs, names, identity = mint_genesis(signer, birth_ts=1_700_000_000.0)
    return manifest, blobs, names, identity, signer


@pytest.fixture
def booted(vclock):
    """A booted Darwin against the full fake stack, plus the stack handle."""
    from darwin_dna.evolution import boot

    platform, stack = make_platform(opening_usdc=60.0, clock=vclock)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    return rt, stack
