"""Real LiteLLM gateway adapter (docs/engineering/13).

These tests avoid real network calls: ``litellm.completion`` and
``litellm.completion_cost`` are injected as test doubles so the suite is
deterministic and free. The transport is exercised through the same
``LLMTransport`` contract the genome uses; if a test passes here, the
genome can use this adapter as a drop-in for ``FakeLLM``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import pytest

from darwin_dna.machinery.llm_router import LLMError, LLMRouter, Tier
from darwin_fakes.fake_wallet import FakeWallet
from darwin_platform import BillingPolicy, Supervisor
from darwin_platform.adapters import LiteLLMTransport, TierMapping


# -- fake LiteLLM doubles --------------------------------------------------
@dataclass
class _FakeMessage:
    content: str


@dataclass
class _FakeChoice:
    message: _FakeMessage


@dataclass
class _FakeUsage:
    cost: float | None = None


class _FakeResponse:
    """Mimics the litellm.completion response object well enough for the
    adapter's extractors. ``usage.cost`` exposes the per-call cost when
    set; otherwise the adapter falls back to ``completion_cost(response)``."""

    def __init__(self, text: str, cost: float | None = None) -> None:
        self.choices = [_FakeChoice(message=_FakeMessage(content=text))]
        self.usage = _FakeUsage(cost=cost)


class _CompletionStub:
    """A callable test double for ``litellm.completion``. Tracks every
    call; lets tests script per-model responses (success / failure)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        # model name -> response | Exception
        self.responses: dict[str, object] = {}
        self.default: object = _FakeResponse("ok", cost=0.001)

    def set(self, model: str, response: object) -> None:
        self.responses[model] = response

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        resp = self.responses.get(kwargs["model"], self.default)
        if isinstance(resp, Exception):
            raise resp
        return resp


def _make_transport(
    wallet: FakeWallet,
    stub: _CompletionStub,
    *,
    markup_rate: float = 0.10,
    chains: dict[str, tuple[str, ...]] | None = None,
) -> LiteLLMTransport:
    chains = chains or {
        "fast": ("provider/fast",),
        "reasoning": ("provider/sonnet", "provider/sonnet-backup"),
        "coding": ("provider/opus",),
        "image_gen": ("provider/img",),
        "embedding": ("provider/embed",),
    }
    return LiteLLMTransport(
        wallet=wallet,
        tier_mappings={k: TierMapping(models=v) for k, v in chains.items()},
        markup_rate=markup_rate,
        litellm_completion=stub,
        litellm_completion_cost=lambda completion_response=None: 0.0,
    )


# -- construction ---------------------------------------------------------
def test_construction_refuses_empty_tier_mappings():
    with pytest.raises(ValueError):
        LiteLLMTransport(
            wallet=FakeWallet(opening_usdc=10.0, clock=lambda: 0.0),
            tier_mappings={},
            litellm_completion=_CompletionStub(),
            litellm_completion_cost=lambda **_: 0.0,
        )


def test_tier_mapping_refuses_empty_chain():
    with pytest.raises(ValueError):
        TierMapping(models=())


def test_negative_markup_refused():
    with pytest.raises(ValueError):
        LiteLLMTransport(
            wallet=FakeWallet(opening_usdc=10.0, clock=lambda: 0.0),
            tier_mappings={"fast": TierMapping(models=("m",))},
            markup_rate=-0.01,
            litellm_completion=_CompletionStub(),
            litellm_completion_cost=lambda **_: 0.0,
        )


# -- task handlers --------------------------------------------------------
def test_plan_handler_basic():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _CompletionStub()
    stub.set("provider/sonnet", _FakeResponse("publish next piece", cost=0.005))
    t = _make_transport(w, stub)

    out = t.complete("reasoning", {
        "task": "plan",
        "strategy_prior": "content_affiliate",
        "balance_usdc": 50.0,
    })
    assert out["text"] == "publish next piece"
    # Cost reported is the provider's, not the marked-up one.
    assert out["cost_usdc"] == pytest.approx(0.005)
    # Wallet was debited at the marked-up rate (10% markup).
    assert w.balance_usdc() == pytest.approx(10.0 - 0.005 * 1.10)


def test_write_long_form_returns_genome_shape():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _CompletionStub()
    body = "# Topic\n\n" + ("Word " * 200)
    stub.set("provider/sonnet", _FakeResponse(body, cost=0.02))
    t = _make_transport(w, stub)

    out = t.complete(
        "reasoning",
        {"task": "write_long_form", "topic": "ergonomic chairs",
         "target_words": 500, "quality": "high"},
    )
    assert out["text"].startswith("# Topic")
    assert out["words"] >= 100
    assert out["quality_score"] == pytest.approx(0.72)  # reasoning default
    assert out["cost_usdc"] == pytest.approx(0.02)


def test_propose_patch_parses_json_response():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _CompletionStub()
    patch_obj = {
        "rationale": "back off the failing path",
        "behavior_delta": {
            "kind": "backoff",
            "failure_class": "captcha",
            "after_failures": 3,
        },
        "changes": [{"path": "soma/x.py", "diff": "+# back off\n"}],
    }
    stub.set("provider/opus", _FakeResponse(json.dumps(patch_obj), cost=0.5))
    t = _make_transport(w, stub)

    out = t.complete("coding", {
        "task": "propose_patch", "failure_class": "captcha",
        "guidance": "fix the recurring failure",
    })
    assert out["rationale"] == patch_obj["rationale"]
    assert out["behavior_delta"]["after_failures"] == 3
    assert out["changes"][0]["path"] == "soma/x.py"
    assert out["cost_usdc"] == pytest.approx(0.5)
    # Wallet debit happened, scaled by markup.
    assert w.balance_usdc() == pytest.approx(10.0 - 0.5 * 1.10)


def test_propose_patch_rejects_non_json():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _CompletionStub()
    stub.set("provider/opus", _FakeResponse("definitely not json", cost=0.01))
    t = _make_transport(w, stub)
    with pytest.raises(LLMError, match="not valid JSON"):
        t.complete("coding", {"task": "propose_patch",
                              "failure_class": "x", "guidance": ""})


def test_propose_patch_rejects_non_object_shape():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _CompletionStub()
    stub.set("provider/opus", _FakeResponse(json.dumps(["a", "b"]), cost=0.01))
    t = _make_transport(w, stub)
    with pytest.raises(LLMError, match="not a JSON object"):
        t.complete("coding", {"task": "propose_patch",
                              "failure_class": "x", "guidance": ""})


# -- unknown tier / task --------------------------------------------------
def test_unknown_tier_raises():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    t = _make_transport(w, _CompletionStub())
    with pytest.raises(LLMError, match="unknown tier"):
        t.complete("ultra", {"task": "plan"})


def test_unknown_task_raises():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    t = _make_transport(w, _CompletionStub())
    with pytest.raises(LLMError, match="unknown task"):
        t.complete("fast", {"task": "summarize"})


def test_missing_tier_chain_raises():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _CompletionStub()
    # Drop the reasoning tier mapping to simulate a deploy-time misconfig.
    t = _make_transport(w, stub, chains={"fast": ("provider/fast",)})
    with pytest.raises(LLMError, match="no model chain"):
        t.complete("reasoning", {"task": "plan"})


# -- fallback chain -------------------------------------------------------
def test_fallback_to_next_model_on_provider_error():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _CompletionStub()
    stub.set("provider/sonnet", RuntimeError("rate limited"))
    stub.set("provider/sonnet-backup",
             _FakeResponse("backup ok", cost=0.003))
    t = _make_transport(w, stub)

    out = t.complete("reasoning", {"task": "plan", "strategy_prior": "x"})
    assert out["text"] == "backup ok"
    # Wallet was debited ONCE — the failed attempt did not bill the Darwin.
    assert w.balance_usdc() == pytest.approx(10.0 - 0.003 * 1.10)
    # Both models were tried (primary + backup) — verify call order.
    assert [c["model"] for c in stub.calls] == [
        "provider/sonnet", "provider/sonnet-backup",
    ]


def test_all_models_failing_raises_llm_error():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _CompletionStub()
    stub.set("provider/sonnet", RuntimeError("down"))
    stub.set("provider/sonnet-backup", RuntimeError("down"))
    t = _make_transport(w, stub)

    with pytest.raises(LLMError, match="every model in tier reasoning failed"):
        t.complete("reasoning", {"task": "plan"})
    assert w.balance_usdc() == pytest.approx(10.0)  # no debit


# -- wallet refusal -------------------------------------------------------
def test_wallet_refusal_surfaces_as_llm_error():
    w = FakeWallet(opening_usdc=0.001, clock=lambda: 0.0)
    stub = _CompletionStub()
    stub.set("provider/sonnet", _FakeResponse("ok", cost=10.0))
    t = _make_transport(w, stub)
    with pytest.raises(LLMError, match="wallet refused"):
        t.complete("reasoning", {"task": "plan"})


# -- cost extraction fallbacks --------------------------------------------
def test_cost_extraction_uses_completion_cost_when_usage_missing():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _CompletionStub()
    # Response has no usage.cost → adapter falls back to completion_cost.
    stub.set("provider/sonnet", _FakeResponse("ok", cost=None))
    calls = []

    def fake_cost(completion_response=None):
        calls.append(completion_response)
        return 0.007

    t = LiteLLMTransport(
        wallet=w,
        tier_mappings={"reasoning": TierMapping(models=("provider/sonnet",))},
        markup_rate=0.10,
        litellm_completion=stub,
        litellm_completion_cost=fake_cost,
    )
    out = t.complete("reasoning", {"task": "plan"})
    assert out["cost_usdc"] == pytest.approx(0.007)
    assert calls, "completion_cost fallback was not invoked"


def test_cost_extraction_returns_zero_when_unknown():
    """If neither path yields a cost, the adapter returns 0 rather than
    crash. Real platform should monitor for this case — it's a data gap."""
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _CompletionStub()
    stub.set("provider/sonnet", _FakeResponse("ok", cost=None))
    t = LiteLLMTransport(
        wallet=w,
        tier_mappings={"reasoning": TierMapping(models=("provider/sonnet",))},
        markup_rate=0.10,
        litellm_completion=stub,
        litellm_completion_cost=lambda **_: (_ for _ in ()).throw(
            RuntimeError("no price table")
        ),
    )
    out = t.complete("reasoning", {"task": "plan"})
    assert out["cost_usdc"] == 0.0


# -- LLMRouter wrapping ---------------------------------------------------
def test_llm_router_accepts_litellm_transport():
    """The adapter must implement the Protocol the router runtime-checks."""
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _CompletionStub()
    stub.set("provider/sonnet", _FakeResponse("hi", cost=0.001))
    t = _make_transport(w, stub)

    router = LLMRouter(t)
    out = router.complete(Tier.REASONING, {"task": "plan"})
    assert out["text"] == "hi"


# -- Supervisor integration ----------------------------------------------
async def test_supervisor_spawns_with_llm_factory() -> None:
    """End-to-end: Supervisor injects the LiteLLMTransport into every
    spawned Darwin via a factory. The Darwin then makes its plan calls
    through the real adapter (here mocked); the genome doesn't notice
    the difference because the transport implements the same protocol."""
    stub = _CompletionStub()
    stub.set("provider/sonnet", _FakeResponse("publish", cost=0.002))
    stub.set("provider/sonnet-backup",
             _FakeResponse("publish", cost=0.002))
    stub.set("provider/fast", _FakeResponse("ok", cost=0.0001))
    stub.set("provider/opus", _FakeResponse("ok", cost=0.01))
    stub.set("provider/img", _FakeResponse("data:img", cost=0.001))
    stub.set("provider/embed", _FakeResponse("[0,1,2]", cost=0.0001))

    def factory(wallet, clock):
        return _make_transport(wallet, stub)

    sup = Supervisor(
        heartbeat_seconds=0.0,
        billing_policy=BillingPolicy(infrastructure_markup_rate=0.10),
        llm_transport_factory=factory,
    )
    try:
        status = await sup.spawn(opening_usdc=200.0)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        # Confirm the override took effect.
        assert isinstance(h.stack.llm, LiteLLMTransport)

        # Drive a few heartbeats to let the planner call the adapter.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if len(stub.calls) > 0:
                break
            import asyncio
            await asyncio.sleep(0.01)
        assert len(stub.calls) > 0, "adapter was never called"
        # The wallet should be debited via the adapter (markup applied).
        assert h.stack.wallet.balance_usdc() < 200.0
    finally:
        await sup.shutdown()
