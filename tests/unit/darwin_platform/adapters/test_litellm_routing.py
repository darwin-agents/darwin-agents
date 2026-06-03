"""LLM-routing sweep additions (docs/engineering/13; audit #23).

Covers the routing/budget behaviours layered onto LiteLLMTransport:

- a per-key *budget* 429 from the proxy short-circuits the fallback chain and
  surfaces as the typed ``LLMBudgetExhausted`` treasury signal — distinct from
  a transient rate-limit 429, which must still fall back;
- the genome's ``LLMRouter`` propagates that typed signal unflattened;
- per-request cache-key scoping by ``darwin_id`` (cross-tenant isolation);
- the structural quality heuristic replacing the old flat constant;
- the shipped LiteLLM proxy configs assert the documented routing knobs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from darwin_dna.machinery.llm_router import (
    LLMBudgetExhausted,
    LLMError,
    LLMRouter,
    Tier,
)
from darwin_fakes.fake_wallet import FakeWallet
from darwin_platform.adapters import LiteLLMTransport, TierMapping
from darwin_platform.adapters.litellm_transport import (
    _grade_long_form,
    _is_budget_exhaustion,
)


# -- doubles --------------------------------------------------------------
@dataclass
class _Msg:
    content: str


@dataclass
class _Choice:
    message: _Msg


@dataclass
class _Usage:
    cost: float | None = None


class _Resp:
    def __init__(self, text: str, cost: float | None = None) -> None:
        self.choices = [_Choice(message=_Msg(content=text))]
        self.usage = _Usage(cost=cost)


class _BudgetError(Exception):
    """Mimics the proxy's per-key budget refusal: HTTP 429 + budget wording."""

    def __init__(self) -> None:
        super().__init__(
            "litellm.RateLimitError: Budget has been exceeded! "
            "Current cost: 5.01, Max budget: 5.00"
        )
        self.status_code = 429


class _RateLimit(Exception):
    """A transient 429 with no budget marker — a fallback model can satisfy it."""

    def __init__(self) -> None:
        super().__init__("litellm.RateLimitError: rate limit reached, slow down")
        self.status_code = 429


class _Stub:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses: dict[str, object] = {}
        self.default: object = _Resp("ok", cost=0.001)

    def set(self, model: str, response: object) -> None:
        self.responses[model] = response

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        resp = self.responses.get(kwargs["model"], self.default)
        if isinstance(resp, Exception):
            raise resp
        return resp


class _AStub(_Stub):
    async def __call__(self, **kwargs):  # type: ignore[override]
        return _Stub.__call__(self, **kwargs)


def _transport(wallet, stub, *, darwin_id=None, **kw) -> LiteLLMTransport:
    return LiteLLMTransport(
        wallet=wallet,
        tier_mappings={
            "reasoning": TierMapping(models=("provider/sonnet", "provider/backup")),
        },
        litellm_completion=stub,
        litellm_completion_cost=lambda completion_response=None: 0.0,
        darwin_id=darwin_id,
        **kw,
    )


# -- budget-429 → typed treasury signal -----------------------------------
def test_budget_429_short_circuits_chain_to_typed_signal():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _Stub()
    stub.set("provider/sonnet", _BudgetError())
    stub.set("provider/backup", _Resp("should not be reached", cost=0.01))
    t = _transport(w, stub)

    with pytest.raises(LLMBudgetExhausted, match="budget exhausted"):
        t.complete("reasoning", {"task": "plan"})
    # The budget is on the key, shared across the chain — the backup must NOT
    # be tried, the wallet must NOT be billed, and this is not a model-health
    # event so the breaker stays closed.
    assert [c["model"] for c in stub.calls] == ["provider/sonnet"]
    assert w.balance_usdc() == pytest.approx(10.0)
    assert t.circuit_breaker.state_of("provider/sonnet") == "closed"


async def test_budget_429_short_circuits_on_async_path():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    astub = _AStub()
    astub.set("provider/sonnet", _BudgetError())
    t = LiteLLMTransport(
        wallet=w,
        tier_mappings={
            "reasoning": TierMapping(models=("provider/sonnet", "provider/backup")),
        },
        litellm_acompletion=astub,
        litellm_completion_cost=lambda completion_response=None: 0.0,
    )
    with pytest.raises(LLMBudgetExhausted):
        await t.acomplete("reasoning", {"task": "plan"})
    assert [c["model"] for c in astub.calls] == ["provider/sonnet"]


def test_plain_rate_limit_429_still_falls_back():
    """A transient 429 without a budget marker is NOT budget exhaustion — the
    chain must degrade to the next model as before."""
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _Stub()
    stub.set("provider/sonnet", _RateLimit())
    stub.set("provider/backup", _Resp("backup ok", cost=0.002))
    t = _transport(w, stub)

    out = t.complete("reasoning", {"task": "plan"})
    assert out["text"] == "backup ok"
    assert [c["model"] for c in stub.calls] == ["provider/sonnet", "provider/backup"]


def test_is_budget_exhaustion_classifier():
    assert _is_budget_exhaustion(_BudgetError()) is True
    assert _is_budget_exhaustion(_RateLimit()) is False
    assert _is_budget_exhaustion(RuntimeError("provider down")) is False
    # A bare BudgetExceededError-shaped class with no status still classifies.
    class BudgetExceededError(Exception):
        pass
    assert _is_budget_exhaustion(BudgetExceededError("budget exceeded")) is True


# -- router preserves the typed signal ------------------------------------
def test_router_propagates_budget_signal_unflattened():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _Stub()
    stub.set("provider/sonnet", _BudgetError())
    router = LLMRouter(_transport(w, stub))
    # Must remain the LLMBudgetExhausted subclass, not be rewrapped into a
    # bare LLMError — the loop relies on the type to route it to the treasury
    # monitor rather than treating it as a generic degrade.
    with pytest.raises(LLMBudgetExhausted):
        router.complete(Tier.REASONING, {"task": "plan"})


# -- cache-key darwin_id scoping ------------------------------------------
def test_cache_namespace_scoped_to_darwin():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _Stub()
    stub.set("provider/sonnet", _Resp("ok", cost=0.001))
    t = _transport(w, stub, darwin_id="darwin-7")
    t.complete("reasoning", {"task": "plan"})
    assert stub.calls[-1]["cache"] == {"namespace": "darwin:darwin-7"}


def test_no_cache_namespace_without_darwin_id():
    w = FakeWallet(opening_usdc=10.0, clock=lambda: 0.0)
    stub = _Stub()
    t = _transport(w, stub)  # no darwin_id
    t.complete("reasoning", {"task": "plan"})
    assert "cache" not in stub.calls[-1]


# -- quality heuristic ----------------------------------------------------
def test_quality_heuristic_zero_for_empty_body():
    assert _grade_long_form("", 0, 500, Tier.REASONING) == 0.0


def test_quality_heuristic_rewards_heading_and_length_adherence():
    body_on = "# Chairs\n\n" + ("word " * 500)
    on_target = _grade_long_form(body_on, 500, 500, Tier.FAST)
    body_off = "# Chairs\n\n" + ("word " * 50)
    off_target = _grade_long_form(body_off, 50, 500, Tier.FAST)
    no_heading = _grade_long_form("word " * 500, 500, 500, Tier.FAST)
    assert on_target > off_target  # hitting the target beats a short piece
    assert on_target > no_heading  # the heading instruction was followed
    assert 0.0 < off_target < on_target <= 1.0


def test_quality_heuristic_reasoning_tier_bump():
    body = "# T\n\n" + ("word " * 500)
    assert _grade_long_form(body, 500, 500, Tier.REASONING) > _grade_long_form(
        body, 500, 500, Tier.FAST
    )


# -- shipped proxy config contract ----------------------------------------
def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "infra").is_dir() and (parent / "src").is_dir():
            return parent
    raise AssertionError("repo root not found")


CONFIGS = [
    "infra/docker/litellm.config.yaml",
    "infra/terraform/envs/local/litellm.config.yaml",
    "infra/terraform/envs/hetzner/litellm.config.yaml",
]


@pytest.mark.parametrize("rel", CONFIGS)
def test_litellm_config_routing_knobs(rel):
    yaml = pytest.importorskip("yaml")
    cfg = yaml.safe_load((_repo_root() / rel).read_text())
    router = cfg["router_settings"]
    # doc 13 §"Why LiteLLM": cost-based, not latency-based.
    assert router["routing_strategy"] == "cost-based-routing"
    # doc 13 §"Failover": one-retry budget.
    assert router["num_retries"] == 1
    # tier=balanced exists as a routing target + soft degrade for reasoning.
    names = {m["model_name"] for m in cfg["model_list"]}
    assert "tier-balanced" in names
    fb = {k: v for d in router["fallbacks"] for k, v in d.items()}
    assert "tier-balanced" in fb["tier-reasoning"]
    # doc 13 §Observability: Prometheus exporter enabled.
    assert "prometheus" in cfg["litellm_settings"]["callbacks"]
