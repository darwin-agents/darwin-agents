"""Tests for the round-3 neurons: accounts.manager, ads.manager,
trading.operator, support.responder (doc 04 full catalog)."""

from __future__ import annotations

import types

import pytest

from darwin_dna.neurons import Task
from darwin_dna.neurons._base import NeuronContext
from darwin_dna.neurons.accounts_manager import AccountsManager
from darwin_dna.neurons.ads_manager import AdsManager
from darwin_dna.neurons.support_responder import SupportResponder
from darwin_dna.neurons.trading_operator import TradingOperator


class _Audit:
    def __init__(self): self.events = []
    def log_intent(self, k, p): self.events.append(("intent", k, p)); return "i-1"
    def log_outcome(self, i, s, p): self.events.append(("outcome", s, p))
    def log_event(self, k, p): self.events.append(("event", k, p))


class _LLM:
    def __init__(self, responses): self.responses = responses
    def complete(self, tier, request):
        return self.responses.get(request.get("task"), {"text": ""})


class _HTTP:
    def __init__(self, status=200, body=""):
        self.status, self.body = status, body
        self.calls = []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return types.SimpleNamespace(status_code=self.status, text=self.body)


class _Browser:
    def __init__(self, outcome): self.outcome = outcome
    def run(self, steps): return self.outcome


class _Vault:
    def __init__(self): self.puts = []
    def put(self, k, v, **kwargs): self.puts.append((k, v, kwargs))


def _ctx(*, http=None, llm=None, browser=None, vault=None):
    ctx = NeuronContext(darwin_id="d-1", llm=llm or _LLM({}), audit=_Audit(),
                        vault=vault, http=http)
    if browser is not None:
        ctx.browser = browser
    return ctx


# ---------------------------------------------------------------------------
# Catalog smoke test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_catalog_loads():
    from darwin_fakes import make_platform
    from darwin_dna.evolution import boot

    _, stack = make_platform()
    runtime = boot(stack.platform())
    assert "accounts.manager" in runtime.catalog
    assert "ads.manager" in runtime.catalog
    assert "trading.operator" in runtime.catalog
    assert "support.responder" in runtime.catalog


# ---------------------------------------------------------------------------
# accounts.manager
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_accounts_signup_via_api_persists_token():
    n = AccountsManager()
    http = _HTTP(status=200)
    vault = _Vault()
    ctx = _ctx(http=http, vault=vault)
    task = Task(
        id="t", kind="signup",
        payload={
            "action": "signup", "service": "stripe",
            "endpoint": "https://stripe.example/accounts",
            "credentials_brief": {"email": "x@x", "token": "tok_abc"},
        },
    )
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is True
    assert result.output["via"] == "api"
    # Token persisted under the scoped path.
    assert any("accounts:stripe:token" in p[0] for p in vault.puts)


@pytest.mark.asyncio
async def test_accounts_browser_fallback_escalates_on_captcha():
    n = AccountsManager()
    browser = _Browser({"blocked": "captcha", "detail": "hCaptcha"})
    ctx = _ctx(browser=browser)
    task = Task(
        id="t", kind="signup",
        payload={
            "action": "signup", "service": "x",
            "browser_steps": ["open https://x.example/signup"],
        },
    )
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.escalated is True
    assert result.error_code == "signup_blocked"


@pytest.mark.asyncio
async def test_accounts_no_endpoint_or_browser_refuses():
    n = AccountsManager()
    ctx = _ctx()
    task = Task(id="t", kind="signup",
                payload={"action": "signup", "service": "x"})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is False
    assert result.error_code == "provider_unavailable"


# ---------------------------------------------------------------------------
# ads.manager
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ads_create_campaign_ok():
    n = AdsManager()
    http = _HTTP(status=200)
    ctx = _ctx(http=http)
    task = Task(
        id="t", kind="ads",
        payload={
            "action": "create_campaign", "platform": "meta",
            "endpoint": "https://meta.example/campaigns",
            "campaign": {"budget_usdc": 5.0, "audience": "us-adults"},
        },
    )
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is True


@pytest.mark.asyncio
async def test_ads_account_locked_escalates():
    n = AdsManager()
    http = _HTTP(status=451)
    ctx = _ctx(http=http)
    task = Task(id="t", kind="ads",
                payload={"action": "create_campaign", "platform": "meta",
                         "endpoint": "https://meta.example/"})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.escalated is True
    assert result.error_code == "account_disabled"


@pytest.mark.asyncio
async def test_ads_no_endpoint_returns_unavailable():
    n = AdsManager()
    ctx = _ctx()
    task = Task(id="t", kind="ads",
                payload={"action": "create_campaign", "platform": "meta"})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is False
    assert result.error_code == "ads_api_unavailable"


# ---------------------------------------------------------------------------
# trading.operator
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trading_buy_market_order_ok():
    n = TradingOperator()
    http = _HTTP(status=200)
    ctx = _ctx(http=http)
    task = Task(
        id="t", kind="trade",
        payload={
            "side": "buy", "symbol": "ETH-USD", "size": 0.1,
            "exchange": "coinbase",
            "endpoint": "https://api.coinbase.example/orders",
        },
    )
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is True
    assert result.output["side"] == "buy"


@pytest.mark.asyncio
async def test_trading_zero_size_refused():
    n = TradingOperator()
    ctx = _ctx(http=_HTTP())
    task = Task(id="t", kind="trade",
                payload={"side": "buy", "symbol": "X", "size": 0,
                         "endpoint": "https://example.com/"})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is False
    assert result.error_code == "risk_refused"


@pytest.mark.asyncio
async def test_trading_422_insufficient_balance_branches_correctly():
    n = TradingOperator()
    http = _HTTP(status=422, body='{"reason": "insufficient balance"}')
    ctx = _ctx(http=http)
    task = Task(id="t", kind="trade",
                payload={"side": "buy", "symbol": "X", "size": 0.5,
                         "endpoint": "https://x.example/"})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.error_code == "insufficient_balance"


@pytest.mark.asyncio
async def test_trading_auth_failure_escalates():
    n = TradingOperator()
    http = _HTTP(status=401)
    ctx = _ctx(http=http)
    task = Task(id="t", kind="trade",
                payload={"side": "buy", "symbol": "X", "size": 0.5,
                         "endpoint": "https://x.example/"})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.escalated is True
    assert result.error_code == "auth_required"


# ---------------------------------------------------------------------------
# support.responder
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_support_keyword_filter_escalates_legal():
    n = SupportResponder()
    llm = _LLM({"support_reply": {"text": "irrelevant", "confidence": 0.99}})
    ctx = _ctx(http=_HTTP(), llm=llm)
    task = Task(id="t", kind="support",
                payload={"channel": "email",
                         "incoming_message": "I will sue you and report fraud"})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.escalated is True
    assert result.error_code == "needs_human"


@pytest.mark.asyncio
async def test_support_low_confidence_escalates():
    n = SupportResponder()
    llm = _LLM({"support_reply": {"text": "draft reply", "confidence": 0.4}})
    ctx = _ctx(http=_HTTP(), llm=llm)
    task = Task(id="t", kind="support",
                payload={"channel": "email",
                         "incoming_message": "Where is my order?",
                         "confidence_threshold": 0.7})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.escalated is True
    assert result.error_code == "needs_human"


@pytest.mark.asyncio
async def test_support_high_confidence_delivers():
    n = SupportResponder()
    llm = _LLM({"support_reply": {"text": "Tracking link: ...",
                                  "confidence": 0.92, "cost_usdc": 0.01}})
    http = _HTTP(status=200)
    ctx = _ctx(http=http, llm=llm)
    task = Task(id="t", kind="support",
                payload={"channel": "email",
                         "incoming_message": "Where is my order?",
                         "endpoint": "https://zendesk.example/reply",
                         "thread_id": "th-9"})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is True
    assert result.output["delivered"] is True


@pytest.mark.asyncio
async def test_support_no_endpoint_returns_composed_reply():
    """No channel wired → return the reply for downstream delivery."""
    n = SupportResponder()
    llm = _LLM({"support_reply": {"text": "draft", "confidence": 0.9}})
    ctx = _ctx(llm=llm)  # no http
    task = Task(id="t", kind="support",
                payload={"channel": "email",
                         "incoming_message": "Hello!"})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is True
    assert result.output["delivered"] is False
    assert result.output["reply"] == "draft"
