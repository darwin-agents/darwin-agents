"""NeuronError / EscalationNeeded — the doc-28 error hierarchy.

Covers the two exception classes and how ``dispatch`` honours them: a
neuron may *raise* EscalationNeeded (instead of returning
NeuronResult(escalated=True)) or raise NeuronError for a non-fatal fault.
A plain Exception still falls through to the neuron_crashed path.
"""

from __future__ import annotations

import types

import pytest

from darwin_dna.neurons import Blocker, EscalationNeeded, NeuronError
from darwin_dna.orchestrator_skeleton.nodes import dispatch
from darwin_dna.orchestrator_skeleton.state import OrchestratorState


# -- the exception classes -------------------------------------------------

def test_neuron_error_is_a_catchable_exception():
    e = NeuronError("rate limited by the API", error_code="rate_limited")
    assert e.error_code == "rate_limited"
    # Under Exception (doc 28) — a careless `except Exception` SHOULD catch
    # it; unlike KillSignal it is recoverable, not a HALT.
    assert isinstance(e, Exception)
    caught = False
    try:
        raise NeuronError("x")
    except Exception:  # noqa: BLE001 - the point of the test
        caught = True
    assert caught


def test_neuron_error_default_code():
    assert NeuronError("boom").error_code == "neuron_error"


def test_escalation_needed_carries_its_blocker():
    b = Blocker(kind="approval", detail="owner must sign off")
    e = EscalationNeeded(b, ticket_id="esc-7")
    assert e.blocker is b
    assert e.ticket_id == "esc-7"
    assert "approval" in str(e)
    assert isinstance(e, Exception)


# -- dispatch honours them -------------------------------------------------

class _RaisingNeuron:
    """A neuron whose execute() raises a chosen exception."""

    def __init__(self, name: str, exc: BaseException):
        self.name = name
        self.capability_tags = frozenset()
        self._exc = exc

    async def plan(self, task, ctx):
        return types.SimpleNamespace(payload={})

    async def execute(self, plan, ctx):
        raise self._exc


def _ctx(neuron):
    events: list[tuple[str, dict]] = []
    episodic: list[dict] = []
    posted: list[dict] = []

    class _Audit:
        def log_event(self, kind, payload):
            events.append((kind, payload))

    class _Episodic:
        def record(self, **kw):
            episodic.append(kw)

    class _Escalation:
        def post(self, payload):
            posted.append(payload)

    class _KillSwitch:
        def check(self):
            return None

    ctx = types.SimpleNamespace(
        catalog={neuron.name: neuron},
        audit=_Audit(),
        kill_switch=_KillSwitch(),
        episodic=_Episodic(),
        escalation=_Escalation(),
        clock=lambda: 100.0,
        darwin_id="d-test",
        build_neuron_context=lambda name: types.SimpleNamespace(),
    )
    return ctx, events, episodic, posted


def _state(neuron_name: str) -> OrchestratorState:
    s = OrchestratorState()
    s.plan = {
        "action": "dispatch",
        "neuron": neuron_name,
        "task": {"id": "t1", "kind": "work"},
    }
    return s


@pytest.mark.asyncio
async def test_dispatch_converts_raised_escalation_to_a_parked_ticket():
    blocker = Blocker(kind="approval", detail="needs owner sign-off")
    neuron = _RaisingNeuron("raiser", EscalationNeeded(blocker))
    ctx, events, episodic, posted = _ctx(neuron)
    state = _state("raiser")

    out = await dispatch.run(state, ctx)

    assert out.route == "reflect"
    # Parked, not failed — exactly like NeuronResult(escalated=True).
    assert len(state.open_escalations) == 1
    ticket = next(iter(state.open_escalations))
    assert ticket in state.pending_escalations
    assert state.open_escalations[ticket]["blocker_kind"] == "approval"
    assert any(k == "dispatch.escalation_raised" for k, _ in events)
    assert len(posted) == 1
    assert episodic and episodic[-1]["outcome"] == "escalated"
    # A raised escalation is not a recurring failure.
    assert state.recurring_failures == {}


@pytest.mark.asyncio
async def test_dispatch_honours_a_preminted_ticket_id():
    blocker = Blocker(kind="approval", detail="x")
    neuron = _RaisingNeuron("raiser", EscalationNeeded(blocker, ticket_id="esc-fixed"))
    ctx, _events, _episodic, _posted = _ctx(neuron)
    state = _state("raiser")

    await dispatch.run(state, ctx)
    assert "esc-fixed" in state.open_escalations


@pytest.mark.asyncio
async def test_dispatch_treats_neuron_error_as_non_fatal_failure():
    neuron = _RaisingNeuron(
        "raiser", NeuronError("API 429", error_code="rate_limited")
    )
    ctx, events, episodic, _posted = _ctx(neuron)
    state = _state("raiser")

    out = await dispatch.run(state, ctx)

    assert out.route == "reflect"  # CONTINUE, not HALT
    assert out.alive is True
    # Recurrence is tracked on the carried error_code.
    assert state.recurring_failures.get("rate_limited") == 1
    assert any(k == "dispatch.neuron_error" for k, _ in events)
    assert episodic and episodic[-1]["error_code"] == "rate_limited"


@pytest.mark.asyncio
async def test_dispatch_halts_on_treasury_engine_fault():
    # doc 28: a fault INSIDE the treasury policy engine is a HALT example,
    # not a recoverable neuron crash. It must propagate out of dispatch (so
    # the heartbeat halts) rather than being swallowed as neuron_crashed →
    # CONTINUE, which would keep the organism acting on a money-safety fault.
    from darwin_dna.machinery.treasury_policy import TreasuryEngineError

    neuron = _RaisingNeuron("payer", TreasuryEngineError("policy engine bug"))
    ctx, events, _episodic, _posted = _ctx(neuron)
    state = _state("payer")

    with pytest.raises(TreasuryEngineError):
        await dispatch.run(state, ctx)
    assert any(k == "dispatch.treasury_engine_fault" for k, _ in events)
    # NOT recorded as a recoverable crash.
    assert state.recurring_failures.get("neuron_crashed") is None


@pytest.mark.asyncio
async def test_dispatch_still_catches_a_plain_exception_as_a_crash():
    # Regression: a neuron raising a generic error keeps the old path.
    neuron = _RaisingNeuron("raiser", ValueError("unexpected"))
    ctx, events, _episodic, _posted = _ctx(neuron)
    state = _state("raiser")

    out = await dispatch.run(state, ctx)

    assert out.route == "reflect"
    assert out.alive is True
    assert any(k == "dispatch.neuron_crashed" for k, _ in events)
    assert state.recurring_failures.get("neuron_crashed") == 1
