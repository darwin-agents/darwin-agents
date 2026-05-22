"""Owner runtime controls (docs/engineering/09).

Covers the four owner controls end to end: the wallet spend-lock, the
reproduction block honoured by ``check_reproduction``, and the
disabled-neuron / disabled-category gates honoured by ``dispatch`` — plus
the Supervisor methods and the drive-loop bridge that carries each control
into a running Darwin's working memory.
"""

from __future__ import annotations

import asyncio
import time
import types
from typing import Callable

import pytest

from darwin_dna.hyperparameters import Hyperparameters
from darwin_dna.neurons.content_publisher import ContentPublisher
from darwin_dna.neurons.content_writer import ContentWriter
from darwin_dna.orchestrator_skeleton.nodes import check_reproduction, dispatch
from darwin_dna.orchestrator_skeleton.state import OrchestratorState
from darwin_fakes.fake_wallet import FakeWallet, WalletError
from darwin_platform import Supervisor, SupervisorError


# ---------------------------------------------------------------------------
# 1. Wallet spend-lock
# ---------------------------------------------------------------------------

def _wallet() -> FakeWallet:
    return FakeWallet(opening_usdc=100.0, clock=lambda: 1_000.0)


def test_spend_lock_refuses_outbound_payments():
    w = _wallet()
    w.set_spend_lock(True, reason="owner away")
    with pytest.raises(WalletError) as ei:
        w.pay(to="vendor", amount_usdc=5.0, memo="ad spend")
    assert "spend locked" in str(ei.value)
    assert "owner away" in str(ei.value)


def test_spend_lock_still_allows_metered_billing():
    # A locked Darwin keeps thinking — metered LLM/compute billing is exempt.
    w = _wallet()
    w.set_spend_lock(True)
    w.debit(0.5, "llm:reasoning")  # no raise
    assert w.balance_usdc() == 99.5


def test_spend_unlock_restores_payments():
    w = _wallet()
    w.set_spend_lock(True)
    w.set_spend_lock(False)
    w.pay(to="vendor", amount_usdc=5.0, memo="ad spend")  # no raise
    assert w.spend_lock() == {"spend_locked": False, "reason": None}


def test_spend_lock_is_independent_of_caps():
    # Locking then unlocking must not disturb the owner's chosen caps.
    w = _wallet()
    w.set_policy(per_action_cap_usdc=10.0)
    w.set_spend_lock(True)
    w.set_spend_lock(False)
    assert w.policy()["per_action_cap_usdc"] == 10.0
    w.pay(to="vendor", amount_usdc=9.0, memo="ok")  # within the 10.0 cap
    with pytest.raises(WalletError):
        w.pay(to="vendor", amount_usdc=11.0, memo="over cap")


# ---------------------------------------------------------------------------
# 2. check_reproduction honours the reproduction block
# ---------------------------------------------------------------------------

def _repro_ctx():
    events: list[tuple[str, dict]] = []

    class _Audit:
        def log_event(self, kind, payload):
            events.append((kind, payload))

    ctx = types.SimpleNamespace(hp=Hyperparameters(), audit=_Audit())
    return ctx, events


def _fertile_state(**overrides) -> OrchestratorState:
    """A state that clears the natural-reproduction gate (profit + surplus +
    healthy fitness)."""
    base = dict(
        balance_usdc=100.0,
        profit_baseline_usdc=0.0,
        starting_capital_usdc=50.0,
        distinct_strategies=["content_affiliate"],
    )
    base.update(overrides)
    return OrchestratorState(**base)


def test_check_reproduction_fires_naturally_when_unblocked():
    ctx, _ = _repro_ctx()
    state = check_reproduction.run(_fertile_state(), ctx)
    assert state.reproduce is True


def test_reproduction_block_stops_natural_spawn():
    ctx, events = _repro_ctx()
    state = check_reproduction.run(
        _fertile_state(reproduction_blocked=True), ctx
    )
    assert state.reproduce is False
    kind, payload = events[-1]
    assert kind == "check_reproduction"
    assert payload["blocked"] is True


def test_reproduction_block_does_not_stop_a_forced_spawn():
    # Forcing is an explicit override; blocking is a standing policy against
    # *autonomous* spawning. A platform force still fires.
    ctx, _ = _repro_ctx()
    state = check_reproduction.run(
        _fertile_state(reproduction_blocked=True, force_reproduce=True), ctx
    )
    assert state.reproduce is True


# ---------------------------------------------------------------------------
# 3. dispatch honours disabled neurons / categories
# ---------------------------------------------------------------------------

def _dispatch_ctx():
    events: list[tuple[str, dict]] = []

    class _Audit:
        def log_event(self, kind, payload):
            events.append((kind, payload))

    class _KillSwitch:
        def check(self):
            return None

    # A minimal catalog is enough to exercise selection: only
    # ContentPublisher carries the ``content.publish`` tag.
    catalog = {
        "content.publisher": ContentPublisher(),
        "content.writer": ContentWriter(),
    }
    ctx = types.SimpleNamespace(
        catalog=catalog, audit=_Audit(), kill_switch=_KillSwitch(),
    )
    return ctx, events


async def test_dispatch_skips_a_disabled_category():
    ctx, events = _dispatch_ctx()
    state = OrchestratorState(disabled_categories=["content.blog"])
    state.plan = {
        "action": "dispatch",
        "task": {"id": "t1", "kind": "publish", "category": "content.blog"},
    }
    out = await dispatch.run(state, ctx)
    assert out.route == "check_reproduction"
    assert any(k == "dispatch.category_disabled" for k, _ in events)


async def test_dispatch_drops_a_disabled_neuron():
    ctx, events = _dispatch_ctx()
    state = OrchestratorState(disabled_neurons=["content.publisher"])
    state.plan = {
        "action": "dispatch",
        "task": {
            "id": "t1", "kind": "publish",
            "required_tags": ["content.publish"],
        },
    }
    out = await dispatch.run(state, ctx)
    assert out.route == "check_reproduction"
    # The unfiltered catalog *would* have matched — so this is recorded as an
    # owner decision, not a capability gap.
    assert any(k == "dispatch.neuron_disabled" for k, _ in events)
    assert not any(k == "capability_request" for k, _ in events)


# ---------------------------------------------------------------------------
# 4. Supervisor methods + the drive-loop bridge
# ---------------------------------------------------------------------------

async def _wait_until(
    pred: Callable[[], bool], *, timeout: float = 5.0, tick: float = 0.01
) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(tick)


@pytest.fixture
async def supervisor():
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        yield sup
    finally:
        await sup.shutdown()


async def test_supervisor_spend_lock_round_trip(supervisor: Supervisor):
    status = await supervisor.spawn(opening_usdc=80.0)
    h = supervisor._darwins[status.darwin_id]  # noqa: SLF001

    res = await supervisor.set_spend_lock(status.darwin_id, locked=True)
    assert res["spend_locked"] is True
    # The wallet now refuses an outbound payment but not metered billing.
    with pytest.raises(WalletError):
        h.stack.wallet.pay(to="vendor", amount_usdc=1.0, memo="x")
    h.stack.wallet.debit(0.1, "llm")  # no raise

    await supervisor.set_spend_lock(status.darwin_id, locked=False)
    assert supervisor.get_owner_controls(
        status.darwin_id
    )["spend_lock"]["spend_locked"] is False


async def test_supervisor_reproduction_block_bridges_to_genome(
    supervisor: Supervisor,
):
    status = await supervisor.spawn(opening_usdc=80.0)
    h = supervisor._darwins[status.darwin_id]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.tick >= 5)

    await supervisor.set_reproduction_block(status.darwin_id, blocked=True)
    await _wait_until(lambda: h.runtime.state.reproduction_blocked is True)
    assert supervisor.get_owner_controls(
        status.darwin_id
    )["reproduction_blocked"] is True

    await supervisor.set_reproduction_block(status.darwin_id, blocked=False)
    await _wait_until(lambda: h.runtime.state.reproduction_blocked is False)


async def test_supervisor_disabled_lists_bridge_to_genome(
    supervisor: Supervisor,
):
    status = await supervisor.spawn(opening_usdc=80.0)
    h = supervisor._darwins[status.darwin_id]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.tick >= 5)

    await supervisor.set_disabled_neurons(
        status.darwin_id, neurons=["content.publisher", "ads.manager"]
    )
    await supervisor.set_disabled_categories(
        status.darwin_id, categories=["trading.crypto"]
    )
    await _wait_until(
        lambda: h.runtime.state.disabled_neurons
        == ["ads.manager", "content.publisher"]
    )
    await _wait_until(
        lambda: h.runtime.state.disabled_categories == ["trading.crypto"]
    )
    controls = supervisor.get_owner_controls(status.darwin_id)
    assert controls["disabled_neurons"] == ["ads.manager", "content.publisher"]
    assert controls["disabled_categories"] == ["trading.crypto"]


async def test_reproduction_block_refused_on_killed_darwin(
    supervisor: Supervisor,
):
    status = await supervisor.spawn(opening_usdc=80.0)
    await supervisor.kill(status.darwin_id, reason="test")
    await _wait_until(
        lambda: not supervisor._darwins[status.darwin_id].runtime.state.alive  # noqa: SLF001
    )
    with pytest.raises(SupervisorError):
        await supervisor.set_reproduction_block(status.darwin_id, blocked=True)
