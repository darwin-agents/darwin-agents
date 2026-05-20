"""LLM circuit breaker tests (doc 13).

A bad provider that keeps failing should not be re-tried indefinitely;
the breaker opens after N consecutive failures, skips during cooldown,
allows one half-open trial, and closes on success.
"""

from __future__ import annotations

import pytest

from darwin_platform.adapters.litellm_transport import (
    CircuitBreaker,
    LiteLLMTransport,
    TierMapping,
)


class _Wallet:
    def debit(self, amount, memo): pass
    def movements(self): return []
    def balance_usdc(self): return 100.0


class _Resp:
    """LiteLLM-shaped response stub."""

    def __init__(self, text="ok"):
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]
        self.usage = type("U", (), {"cost": 0.001})()


def test_breaker_starts_closed():
    cb = CircuitBreaker(failure_threshold=3, open_seconds=10)
    assert cb.can_call("m1") is True
    assert cb.state_of("m1") == "closed"


def test_breaker_opens_after_threshold():
    cb = CircuitBreaker(failure_threshold=3, open_seconds=10)
    cb.record_failure("m1")
    cb.record_failure("m1")
    assert cb.state_of("m1") == "closed"  # below threshold
    cb.record_failure("m1")
    assert cb.state_of("m1") == "open"
    assert cb.can_call("m1") is False


def test_breaker_half_open_after_cooldown():
    now = [1000.0]
    cb = CircuitBreaker(failure_threshold=2, open_seconds=10, clock=lambda: now[0])
    cb.record_failure("m1")
    cb.record_failure("m1")
    assert cb.state_of("m1") == "open"
    now[0] = 1020.0
    assert cb.can_call("m1") is True
    assert cb.state_of("m1") == "half_open"


def test_half_open_failure_reopens():
    now = [1000.0]
    cb = CircuitBreaker(failure_threshold=2, open_seconds=10, clock=lambda: now[0])
    cb.record_failure("m1")
    cb.record_failure("m1")
    now[0] = 1020.0
    cb.can_call("m1")  # half_open
    cb.record_failure("m1")  # trial failed
    assert cb.state_of("m1") == "open"
    # And we're back to skipping
    assert cb.can_call("m1") is False


def test_success_closes_breaker():
    cb = CircuitBreaker(failure_threshold=2)
    cb.record_failure("m1")
    cb.record_failure("m1")
    cb.record_success("m1")
    assert cb.state_of("m1") == "closed"


def test_transport_skips_open_models_falls_back():
    """A model with an open circuit should be skipped; the next model
    in the fallback chain is tried."""
    cb = CircuitBreaker(failure_threshold=1, open_seconds=60)
    # Pre-trip model-1 so the transport must use model-2.
    cb.record_failure("model-1")
    assert cb.can_call("model-1") is False

    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        return _Resp("hi")

    t = LiteLLMTransport(
        wallet=_Wallet(),
        tier_mappings={"fast": TierMapping(models=("model-1", "model-2"))},
        litellm_completion=fake_completion,
        litellm_completion_cost=lambda r: 0.0,
        circuit_breaker=cb,
    )
    out = t.complete("fast", {"task": "plan"})
    assert out["text"] == "hi"
    assert calls == ["model-2"]  # model-1 was skipped


def test_transport_records_failures_into_breaker():
    """Each provider exception should mark the model in the breaker."""
    cb = CircuitBreaker(failure_threshold=3, open_seconds=60)
    n = [0]

    def fake_completion(**kwargs):
        n[0] += 1
        raise RuntimeError("rate-limited")

    t = LiteLLMTransport(
        wallet=_Wallet(),
        tier_mappings={"fast": TierMapping(models=("m1",))},
        litellm_completion=fake_completion,
        litellm_completion_cost=lambda r: 0.0,
        circuit_breaker=cb,
    )
    # First three calls each record a failure; the third trips it.
    for _ in range(3):
        with pytest.raises(Exception):
            t.complete("fast", {"task": "plan"})
    assert cb.state_of("m1") == "open"
    # Fourth call: every model open → "every model in tier ... is
    # circuit-open" — distinct from "every model failed".
    with pytest.raises(Exception, match="circuit-open"):
        t.complete("fast", {"task": "plan"})


def test_transport_records_success_closes_breaker():
    cb = CircuitBreaker(failure_threshold=2, open_seconds=60)
    # One earlier failure on this model
    cb.record_failure("m1")

    def fake_completion(**kwargs):
        return _Resp("good")

    t = LiteLLMTransport(
        wallet=_Wallet(),
        tier_mappings={"fast": TierMapping(models=("m1",))},
        litellm_completion=fake_completion,
        litellm_completion_cost=lambda r: 0.0,
        circuit_breaker=cb,
    )
    t.complete("fast", {"task": "plan"})
    # The success resets the counter.
    assert cb.state_of("m1") == "closed"
    snapshot = cb.snapshot()
    assert snapshot["m1"]["consecutive_failures"] == 0
