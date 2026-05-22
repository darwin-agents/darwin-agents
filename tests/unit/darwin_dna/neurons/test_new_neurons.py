"""Tests for the three new neurons (docs/engineering/04 expansion):
content.publisher, analytics.measurer, research.scout. Coverage:
- Each neuron loads through the catalog at boot.
- plan() and execute() conform to the Protocol shape.
- HTTP path (via context.http) and no-endpoint fallback paths.
- Audit events fire on success + failure.
"""

from __future__ import annotations

import types

import pytest

from darwin_dna.neurons import Task
from darwin_dna.neurons._base import NeuronContext
from darwin_dna.neurons.analytics_measurer import AnalyticsMeasurer
from darwin_dna.neurons.content_publisher import ContentPublisher
from darwin_dna.neurons.research_scout import ResearchScout


class _StubAudit:
    def __init__(self):
        self.events = []

    def log_intent(self, kind, payload):
        self.events.append(("intent", kind, payload))
        return f"intent-{len(self.events)}"

    def log_outcome(self, intent, outcome, payload):
        self.events.append(("outcome", outcome, payload))

    def log_event(self, kind, payload):
        self.events.append(("event", kind, payload))


class _StubLLM:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def complete(self, tier, request):
        self.calls.append((tier, request))
        return self.responses.get(request.get("task"), {"text": "", "cost_usdc": 0.0})


class _StubHTTP:
    """Configurable HTTP client. ``request`` returns a SimpleNamespace whose
    fields the neurons probe."""

    def __init__(self, status=200, body="{}"):
        self.status = status
        self.body = body
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return types.SimpleNamespace(status_code=self.status, text=self.body)


def _ctx(*, http=None, llm=None, content_classifier=None):
    return NeuronContext(
        darwin_id="d-test", llm=llm or _StubLLM({}), audit=_StubAudit(),
        http=http, content_classifier=content_classifier,
    )


# ---------------------------------------------------------------------------
# 1. Catalog wiring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_catalog_contains_new_neurons():
    """build_catalog wires the new neurons; they pass the loader's
    Protocol check."""
    from darwin_fakes import make_platform
    from darwin_dna.evolution import boot

    _, stack = make_platform()
    runtime = boot(stack.platform())
    assert "content.publisher" in runtime.catalog
    assert "analytics.measurer" in runtime.catalog
    assert "research.scout" in runtime.catalog


def test_known_gaps_no_longer_include_implemented():
    from darwin_dna.neurons import KNOWN_GAPS
    # The previously-flagged gaps are now in the catalog.
    assert "analytics.measure" not in KNOWN_GAPS
    assert "content.publish_api" not in KNOWN_GAPS


# ---------------------------------------------------------------------------
# 2. ContentPublisher
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publisher_api_path_success():
    n = ContentPublisher()
    http = _StubHTTP(status=201)
    ctx = _ctx(http=http)
    task = Task(
        id="t-1", kind="publish",
        payload={
            "channel": "wordpress",
            "title": "Why X",
            "text": "Long body...",
            "destination_url": "https://wp.example/posts",
        },
    )
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is True
    assert result.output["via"] == "api"
    assert result.output["channel"] == "wordpress"
    # Exactly one POST.
    assert len(http.calls) == 1
    method, url, _ = http.calls[0]
    assert method == "POST"
    assert url == "https://wp.example/posts"


@pytest.mark.asyncio
async def test_publisher_no_http_escalates():
    """No x402 client → escalate so the gap is visible, not silent."""
    n = ContentPublisher()
    ctx = _ctx(http=None)
    task = Task(
        id="t-1", kind="publish",
        payload={"channel": "wordpress", "text": "body"},
    )
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is False
    assert result.escalated is True
    assert result.error_code == "no_channel"


@pytest.mark.asyncio
async def test_publisher_empty_text_refused():
    n = ContentPublisher()
    ctx = _ctx(http=_StubHTTP())
    task = Task(id="t-1", kind="publish",
                payload={"channel": "wordpress", "text": "   "})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is False
    assert result.error_code == "api_unavailable"


@pytest.mark.asyncio
async def test_publisher_auth_failure_escalates():
    n = ContentPublisher()
    http = _StubHTTP(status=401)
    ctx = _ctx(http=http)
    task = Task(id="t-1", kind="publish",
                payload={"channel": "medium", "text": "body",
                         "destination_url": "https://medium.example/api"})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.escalated is True
    assert result.error_code == "auth_required"


@pytest.mark.asyncio
async def test_publisher_blocks_unsafe_content_via_backstop():
    """The deterministic content backstop blocks a flagged publish even with
    no platform classifier wired (doc 10)."""
    n = ContentPublisher()
    http = _StubHTTP(status=201)
    ctx = _ctx(http=http)
    task = Task(
        id="t-1", kind="publish",
        payload={
            "channel": "wordpress",
            "title": "Civic guide",
            "text": "A friendly explainer on how to rig an election.",
            "destination_url": "https://wp.example/posts",
        },
    )
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is False
    assert result.escalated is True
    assert result.error_code == "content_blocked"
    assert "election_interference" in result.output["categories"]
    # Crucially: nothing was published.
    assert len(http.calls) == 0
    assert any(e[1] == "content_blocked" for e in ctx.audit.events)


@pytest.mark.asyncio
async def test_publisher_blocks_when_platform_classifier_flags():
    """A platform classifier verdict blocks publication and escalates."""

    class _Flagging:
        def classify(self, text, *, kind):
            from darwin_dna.machinery.content_safety import ContentVerdict
            return ContentVerdict(
                allowed=False, categories=("policy",),
                detail="flagged", via="classifier",
            )

    n = ContentPublisher()
    http = _StubHTTP(status=201)
    ctx = _ctx(http=http, content_classifier=_Flagging())
    task = Task(
        id="t-1", kind="publish",
        payload={"channel": "wordpress", "text": "totally ordinary copy",
                 "destination_url": "https://wp.example/posts"},
    )
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.error_code == "content_blocked"
    assert result.escalated is True
    assert len(http.calls) == 0


@pytest.mark.asyncio
async def test_publisher_clean_titled_content_still_publishes():
    """Screening title+body must not get in the way of legitimate content."""
    n = ContentPublisher()
    http = _StubHTTP(status=201)
    ctx = _ctx(http=http)
    task = Task(
        id="t-1", kind="publish",
        payload={"channel": "wordpress", "title": "Best hiking boots 2026",
                 "text": "An honest review of trail footwear.",
                 "destination_url": "https://wp.example/posts"},
    )
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is True
    assert len(http.calls) == 1


# ---------------------------------------------------------------------------
# 3. AnalyticsMeasurer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_analytics_no_endpoint_returns_no_endpoint_error():
    n = AnalyticsMeasurer()
    ctx = _ctx(http=None)
    task = Task(id="t-1", kind="measure", payload={"scope": "site"})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    # Returns ok=False but does NOT escalate (reflect can handle empty).
    assert result.ok is False
    assert result.error_code == "no_endpoint"
    assert result.escalated is False


@pytest.mark.asyncio
async def test_analytics_happy_path_returns_roi():
    n = AnalyticsMeasurer()
    http = _StubHTTP(
        status=200,
        body='{"revenue_usdc": 120.0, "cost_usdc": 30.0, "sessions": 4000}',
    )
    llm = _StubLLM({
        "analytics_synthesis": {
            "text": "site: revenue=$120.00, cost=$30.00, roi=3.00",
            "roi": 3.0,
            "cost_usdc": 0.005,
        }
    })
    ctx = _ctx(http=http, llm=llm)
    task = Task(
        id="t-1", kind="measure",
        payload={
            "scope": "site",
            "endpoint": "https://analytics.example/metrics",
            "window_days": 7,
        },
    )
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is True
    assert result.output["roi"] == 3.0
    assert result.output["metrics"]["revenue_usdc"] == 120.0


@pytest.mark.asyncio
async def test_analytics_5xx_fails_without_escalation():
    n = AnalyticsMeasurer()
    http = _StubHTTP(status=503, body="upstream down")
    ctx = _ctx(http=http)
    task = Task(id="t-1", kind="measure",
                payload={"scope": "site",
                         "endpoint": "https://example.com/m"})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is False
    assert result.error_code == "analytics_unavailable"
    assert result.escalated is False


# ---------------------------------------------------------------------------
# 4. ResearchScout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_research_no_endpoint_returns_no_endpoint_error():
    n = ResearchScout()
    ctx = _ctx(http=None)
    # ResearchScout.plan() pre-populates a default endpoint; we override
    # it here to None.
    task = Task(id="t-1", kind="research",
                payload={"query": "best blender 2026", "endpoint": ""})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is False
    assert result.error_code == "no_endpoint"


@pytest.mark.asyncio
async def test_research_happy_path_summarises():
    n = ResearchScout()
    http = _StubHTTP(
        status=200,
        body='{"results": [{"title": "Best blenders 2026", "url": "x"}]}',
    )
    llm = _StubLLM({
        "research_synthesis": {
            "text": "Summary of 1 result for 'best blender 2026': Best blenders 2026",
            "cost_usdc": 0.01,
        }
    })
    ctx = _ctx(http=http, llm=llm)
    task = Task(
        id="t-1", kind="research",
        payload={"query": "best blender 2026",
                 "endpoint": "https://search.example/search", "k": 5},
    )
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is True
    assert result.output["n_results"] == 1
    assert "Best blenders 2026" in result.output["summary"]


@pytest.mark.asyncio
async def test_research_empty_query_refused():
    n = ResearchScout()
    ctx = _ctx(http=_StubHTTP())
    task = Task(id="t-1", kind="research", payload={"query": "   "})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is False
    assert result.error_code == "search_unavailable"


@pytest.mark.asyncio
async def test_research_empty_synthesis_returns_specific_error():
    n = ResearchScout()
    http = _StubHTTP(status=200, body='{"results": [{"title": "x"}]}')
    llm = _StubLLM({"research_synthesis": {"text": "", "cost_usdc": 0.0}})
    ctx = _ctx(http=http, llm=llm)
    task = Task(id="t-1", kind="research",
                payload={"query": "anything",
                         "endpoint": "https://search.example/"})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is False
    assert result.error_code == "empty_synthesis"
