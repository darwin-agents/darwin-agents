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


# -- per-tier timeouts (doc 28) -------------------------------------------
def test_per_tier_timeout_applied_to_completion_call():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _CompletionStub()
    t = _make_transport(w, stub)
    req = {"task": "plan", "strategy_prior": "x", "balance_usdc": 1.0}

    t.complete("fast", req)
    assert stub.calls[-1]["timeout"] == 30.0
    t.complete("reasoning", req)
    assert stub.calls[-1]["timeout"] == 120.0
    t.complete("coding", req)
    assert stub.calls[-1]["timeout"] == 300.0


def test_tiers_without_a_configured_timeout_fall_back_to_default():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _CompletionStub()
    t = _make_transport(w, stub)
    # image_gen / embedding are not in the doc-28 table -> request_timeout.
    t.complete("image_gen", {"task": "plan", "balance_usdc": 1.0})
    assert stub.calls[-1]["timeout"] == 60.0


def test_tier_timeouts_can_be_overridden():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _CompletionStub()
    t = LiteLLMTransport(
        wallet=w,
        tier_mappings={"fast": TierMapping(models=("m",))},
        litellm_completion=stub,
        litellm_completion_cost=lambda **_: 0.0,
        tier_timeouts={"fast": 5.0},
    )
    t.complete("fast", {"task": "plan", "balance_usdc": 1.0})
    assert stub.calls[-1]["timeout"] == 5.0


# -- per-Darwin attribution metadata (doc 13) ----------------------------
def test_darwin_id_metadata_attached_to_each_call():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _CompletionStub()
    t = LiteLLMTransport(
        wallet=w,
        tier_mappings={"reasoning": TierMapping(models=("m",))},
        litellm_completion=stub,
        litellm_completion_cost=lambda **_: 0.0,
        darwin_id="darwin-42",
    )
    t.complete("reasoning", {"task": "plan", "balance_usdc": 1.0})
    # The proxy keys its spend table + cache on metadata.darwin_id; tier
    # rides along for per-tier post-hoc attribution.
    assert stub.calls[-1]["metadata"] == {
        "darwin_id": "darwin-42",
        "tier": "reasoning",
    }


def test_no_metadata_when_darwin_id_unset():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _CompletionStub()
    t = _make_transport(w, stub)  # no darwin_id
    t.complete("fast", {"task": "plan", "balance_usdc": 1.0})
    assert "metadata" not in stub.calls[-1]


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


# -- async-isolated provider call (BUG-drive-loop-tick1-freeze) -----------
class _ACompletionStub:
    """Async test double for ``litellm.acompletion``. The real gateway is
    driven through the async client on an isolated worker-loop; this lets us
    exercise that path without litellm or the network."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.thread_names: list[str] = []
        self.responses: dict[str, object] = {}
        self.default: object = _FakeResponse("ok", cost=0.001)

    def set(self, model: str, response: object) -> None:
        self.responses[model] = response

    async def __call__(self, **kwargs):
        import threading

        self.calls.append(kwargs)
        self.thread_names.append(threading.current_thread().name)
        resp = self.responses.get(kwargs["model"], self.default)
        if isinstance(resp, Exception):
            raise resp
        return resp


def _make_async_transport(
    wallet: FakeWallet, astub: _ACompletionStub
) -> LiteLLMTransport:
    # No sync ``litellm_completion`` injected → the async path is used and the
    # sync completion is the loud guard (must never be reached).
    return LiteLLMTransport(
        wallet=wallet,
        tier_mappings={
            "reasoning": TierMapping(models=("provider/sonnet", "provider/backup")),
        },
        litellm_acompletion=astub,
        litellm_completion_cost=lambda completion_response=None: 0.0,
    )


def test_async_path_runs_off_the_calling_loop_and_returns():
    """The real gateway path drives ``acompletion`` on a dedicated worker
    thread (its own event loop), never the calling thread — that is what
    keeps it from re-entering the supervisor's running loop."""
    import threading

    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    astub = _ACompletionStub()
    astub.set("provider/sonnet", _FakeResponse("planned", cost=0.004))
    t = _make_async_transport(w, astub)

    out = t.complete("reasoning", {"task": "plan", "strategy_prior": "x"})
    assert out["text"] == "planned"
    assert out["cost_usdc"] == pytest.approx(0.004)
    assert w.balance_usdc() == pytest.approx(10.0 - 0.004 * 1.10)
    # The provider call ran on a worker thread, not this (main) one.
    assert astub.thread_names and astub.thread_names[0] != threading.current_thread().name


def test_async_path_runs_inside_a_running_event_loop():
    """The whole point: calling ``complete`` from *inside* a live asyncio
    loop must still work (this is the supervisor's situation) — the worker
    loop is independent, so no re-entrancy is required."""
    import asyncio

    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    astub = _ACompletionStub()
    astub.set("provider/sonnet", _FakeResponse("ok-in-loop", cost=0.001))
    t = _make_async_transport(w, astub)

    async def driver():
        # Run the blocking sync ``complete`` off the loop thread, exactly as
        # the supervisor's sync node path does NOT — but this proves the
        # worker-loop call completes and returns under a running loop.
        return await asyncio.to_thread(
            t.complete, "reasoning", {"task": "plan"}
        )

    out = asyncio.run(driver())
    assert out["text"] == "ok-in-loop"


def test_async_path_provider_error_falls_back_to_next_model():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    astub = _ACompletionStub()
    astub.set("provider/sonnet", RuntimeError("rate limited"))
    astub.set("provider/backup", _FakeResponse("backup ok", cost=0.003))
    t = _make_async_transport(w, astub)

    out = t.complete("reasoning", {"task": "plan"})
    assert out["text"] == "backup ok"
    assert [c["model"] for c in astub.calls] == ["provider/sonnet", "provider/backup"]


def test_sync_completion_guard_raises_when_only_async_injected():
    """When only the async path is wired, the sync completion is a loud
    guard — a regression that re-introduces the sync call surfaces here
    instead of silently re-entering the loop."""
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    t = _make_async_transport(w, _ACompletionStub())
    with pytest.raises(LLMError, match="synchronous litellm.completion is unavailable"):
        t._completion(model="x", messages=[])  # noqa: SLF001


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
