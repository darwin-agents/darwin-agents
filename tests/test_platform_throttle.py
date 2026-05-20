"""Throttle infrastructure (docs/engineering/09 level 2).

Before this slice: the Monitor emitted WARN alerts but the platform took
no action — operators were expected to act manually. After: the same
LLM tier the genome is already enforcing per-day caps on can be tightened
by the platform at runtime, optionally auto-fired on a WARN.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

import pytest

from darwin_dna.machinery.llm_router import (
    LLMRouter,
    Tier,
    TierBudgetExceeded,
    hp_cap_for_tier,
)
from darwin_dna.hyperparameters import DEFAULT_HYPERPARAMETERS
from darwin_platform import Quotas, Supervisor, Severity


async def _wait_until(
    pred: Callable[[], bool], *, timeout: float = 5.0, tick: float = 0.01
) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(tick)


# -- LLMRouter: throttle takes min(hp, throttle) --------------------------
class _StubTransport:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, tier, request):
        self.calls += 1
        return {"text": "ok"}


def test_throttle_takes_min_of_hp_and_installed_cap():
    t = _StubTransport()
    r = LLMRouter(t)
    hp_cap = hp_cap_for_tier(DEFAULT_HYPERPARAMETERS, Tier.REASONING)
    assert hp_cap is not None
    # Install a tighter throttle.
    r.set_throttle(Tier.REASONING, 3)
    assert r.remaining(Tier.REASONING) == 3
    r.complete(Tier.REASONING, {})
    r.complete(Tier.REASONING, {})
    r.complete(Tier.REASONING, {})
    assert r.remaining(Tier.REASONING) == 0
    with pytest.raises(TierBudgetExceeded):
        r.complete(Tier.REASONING, {})


def test_throttle_cannot_loosen_hp_cap():
    """Installing a throttle higher than the hp cap is silently ignored —
    ``remaining`` still takes the min."""
    t = _StubTransport()
    r = LLMRouter(t)
    hp_cap = hp_cap_for_tier(DEFAULT_HYPERPARAMETERS, Tier.REASONING)
    r.set_throttle(Tier.REASONING, hp_cap * 100)
    assert r.remaining(Tier.REASONING) == hp_cap


def test_clear_throttle_restores_hp_cap():
    t = _StubTransport()
    r = LLMRouter(t)
    hp_cap = hp_cap_for_tier(DEFAULT_HYPERPARAMETERS, Tier.REASONING)
    r.set_throttle(Tier.REASONING, 1)
    r.complete(Tier.REASONING, {})
    with pytest.raises(TierBudgetExceeded):
        r.complete(Tier.REASONING, {})
    r.clear_throttle(Tier.REASONING)
    # The already-spent call still counts; hp cap is now binding.
    assert r.remaining(Tier.REASONING) == hp_cap - 1


def test_clear_throttle_all_tiers():
    t = _StubTransport()
    r = LLMRouter(t)
    r.set_throttle(Tier.REASONING, 5)
    r.set_throttle(Tier.CODING, 1)
    r.clear_throttle()  # clear all
    assert r.throttles() == {}


def test_set_throttle_rejects_negative():
    t = _StubTransport()
    r = LLMRouter(t)
    with pytest.raises(ValueError):
        r.set_throttle(Tier.REASONING, -1)


# -- Supervisor surface ---------------------------------------------------
@pytest.fixture
async def supervisor():
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        yield sup
    finally:
        await sup.shutdown()


async def test_supervisor_install_and_clear_throttle(supervisor: Supervisor) -> None:
    status = await supervisor.spawn(opening_usdc=60.0)
    out = await supervisor.throttle_llm_tier(
        status.darwin_id, tier="reasoning", max_daily_calls=2
    )
    assert out["tier"] == "reasoning"
    assert out["max_daily_calls"] == 2
    assert out["throttles"]["reasoning"] == 2

    h = supervisor._darwins[status.darwin_id]  # noqa: SLF001
    assert h.runtime.llm.remaining(Tier.REASONING) == 2

    out = await supervisor.clear_llm_throttle(status.darwin_id, tier="reasoning")
    assert "reasoning" not in out["throttles"]


async def test_supervisor_rejects_unknown_tier(supervisor: Supervisor) -> None:
    from darwin_platform import SupervisorError

    status = await supervisor.spawn(opening_usdc=60.0)
    with pytest.raises(SupervisorError, match="unknown tier"):
        await supervisor.throttle_llm_tier(
            status.darwin_id, tier="ultrafast", max_daily_calls=5
        )


# -- auto-throttle on WARN ------------------------------------------------
async def test_auto_throttle_on_warn_spend_velocity() -> None:
    """A WARN spend_velocity halves the reasoning tier cap. Idempotent —
    a sustained breach does NOT re-throttle (the latch's the point)."""
    quotas = Quotas(
        spend_velocity_usd_per_hour_warn=10.0,
        spend_velocity_usd_per_hour_critical=1_000_000.0,
        failure_rate_warn=1.1,  # disabled
        failure_rate_critical=1.1,
        heartbeat_warn_gap_seconds=1_000_000.0,
        heartbeat_critical_gap_seconds=1_000_000.0,
        escalation_rate_per_hour_warn=1_000_000,
        escalation_rate_per_hour_critical=1_000_000,
        dedup_window_seconds=0.0,
    )
    sup = Supervisor(
        heartbeat_seconds=0.0,
        quotas=quotas,
        monitor_every_n_ticks=1,
        auto_throttle_on_warn=True,
    )
    try:
        status = await sup.spawn(opening_usdc=200.0)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        # Stage spend movements in the wallet ledger so the spend_velocity
        # detector trips on the next monitor pass.
        now = time.time()
        for _ in range(5):
            h.stack.wallet.ledger.append(
                {"debit": {"amount_usdc": 5.0, "ts": now, "kind": "metered"}}
            )
        await _wait_until(
            lambda: "spend_velocity" in h.auto_throttled,
            timeout=5.0,
        )
        # Throttle is now in place on the reasoning tier (the mapped tier).
        assert h.runtime.llm.throttles().get("reasoning") is not None

        cap_before = h.runtime.llm.throttles()["reasoning"]
        # Add more debits: the latch prevents re-throttling.
        for _ in range(5):
            h.stack.wallet.ledger.append(
                {"debit": {"amount_usdc": 5.0, "ts": now, "kind": "metered"}}
            )
        await asyncio.sleep(0.05)
        assert h.runtime.llm.throttles()["reasoning"] == cap_before
    finally:
        await sup.shutdown()


async def test_auto_throttle_off_by_default() -> None:
    """Without auto_throttle_on_warn, a WARN alert fires but nothing is
    throttled. The operator stays in the loop."""
    quotas = Quotas(
        spend_velocity_usd_per_hour_warn=10.0,
        spend_velocity_usd_per_hour_critical=1_000_000.0,
        failure_rate_warn=1.1,
        failure_rate_critical=1.1,
        heartbeat_warn_gap_seconds=1_000_000.0,
        heartbeat_critical_gap_seconds=1_000_000.0,
        escalation_rate_per_hour_warn=1_000_000,
        escalation_rate_per_hour_critical=1_000_000,
        dedup_window_seconds=0.0,
    )
    sup = Supervisor(
        heartbeat_seconds=0.0, quotas=quotas, monitor_every_n_ticks=1
    )
    try:
        status = await sup.spawn(opening_usdc=200.0)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        now = time.time()
        for _ in range(5):
            h.stack.wallet.ledger.append(
                {"debit": {"amount_usdc": 5.0, "ts": now, "kind": "metered"}}
            )
        await _wait_until(
            lambda: any(a.severity is Severity.WARN
                        for a in sup.monitor.alerts_for(status.darwin_id))
        )
        # Alert fired; throttle is empty.
        assert h.runtime.llm.throttles() == {}
        assert "spend_velocity" not in h.auto_throttled
    finally:
        await sup.shutdown()


# -- HTTP endpoints -------------------------------------------------------
async def test_throttle_endpoints() -> None:
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from darwin_platform.api import build_app  # type: ignore

    sup = Supervisor(heartbeat_seconds=0.0)
    app = build_app(sup)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            r = await c.post("/darwins", json={"opening_usdc": 60.0})
            did = r.json()["darwin_id"]

            r = await c.post(
                f"/platform/darwins/{did}/throttle",
                json={"tier": "coding", "max_daily_calls": 3},
            )
            assert r.status_code == 200
            assert r.json()["throttles"]["coding"] == 3

            r = await c.get(f"/platform/darwins/{did}/throttle")
            assert r.json()["throttles"]["coding"] == 3

            # Unknown tier → 422 (pydantic regex).
            r = await c.post(
                f"/platform/darwins/{did}/throttle",
                json={"tier": "ultrafast", "max_daily_calls": 5},
            )
            assert r.status_code == 422

            # Negative cap → 422.
            r = await c.post(
                f"/platform/darwins/{did}/throttle",
                json={"tier": "fast", "max_daily_calls": -1},
            )
            assert r.status_code == 422

            r = await c.delete(
                f"/platform/darwins/{did}/throttle", params={"tier": "coding"}
            )
            assert r.status_code == 200
            assert "coding" not in r.json()["throttles"]

            # Clear all.
            await c.post(
                f"/platform/darwins/{did}/throttle",
                json={"tier": "coding", "max_daily_calls": 3},
            )
            r = await c.delete(f"/platform/darwins/{did}/throttle")
            assert r.json()["throttles"] == {}

            # Unknown id → 404.
            r = await c.post(
                "/platform/darwins/nope/throttle",
                json={"tier": "coding", "max_daily_calls": 5},
            )
            assert r.status_code == 404
        finally:
            await sup.shutdown()
