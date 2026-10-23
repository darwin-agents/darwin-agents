"""OpenTelemetry tracing assertions (docs/engineering/08 §Observability).

Configures the OTel SDK with an InMemorySpanExporter so we can directly
inspect what the Supervisor recorded. Skipped if OTel isn't installed.

Doc 08: "Spans are tagged with darwin_id, lineage_root_id, generation, and
per-event metadata." Each test below asserts on at least one of those tags
to keep the trace shape useful for the dashboard's Timeline view.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

import pytest

pytest.importorskip("opentelemetry")

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from darwin_platform import Supervisor


async def _wait_until(
    pred: Callable[[], bool], *, timeout: float = 5.0, tick: float = 0.01
) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(tick)


_PROVIDER_INSTALLED = False


@pytest.fixture
def exporter():
    """Install (once) a global TracerProvider with an InMemorySpanExporter.
    OTel's API caches the provider so we must reuse it across tests; the
    exporter itself is cleared between tests so each test sees only its
    own spans."""
    global _PROVIDER_INSTALLED
    exp = InMemorySpanExporter()
    if not _PROVIDER_INSTALLED:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exp))
        trace.set_tracer_provider(provider)
        _PROVIDER_INSTALLED = True
    else:
        trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exp))
    exp.clear()
    yield exp
    exp.clear()


def _names(spans):
    return [s.name for s in spans]


def _by_name(spans, name):
    return [s for s in spans if s.name == name]


async def test_spawn_emits_span_with_doc08_tags(exporter) -> None:
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        status = await sup.spawn(opening_usdc=80.0)
        # Force the exporter to flush; SimpleSpanProcessor does so on span
        # end, but a brief yield ensures the drive task spans aren't pending.
        await asyncio.sleep(0)
        spans = exporter.get_finished_spans()
        spawns = _by_name(spans, "supervisor.spawn")
        assert spawns, _names(spans)
        s = spawns[-1]
        # darwin_id is set after the runtime is wired; generation + lineage
        # come from annotate_span_with_runtime.
        assert s.attributes["darwin_id"] == status.darwin_id
        assert s.attributes["generation"] == 0
        assert s.attributes["lineage_root_id"] == status.darwin_id
        assert s.attributes["opening_usdc"] == pytest.approx(80.0)
    finally:
        await sup.shutdown()


async def test_pause_resume_kill_each_emit_a_span(exporter) -> None:
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        status = await sup.spawn(opening_usdc=60.0)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        await _wait_until(lambda: h.runtime.state.tick >= 3)

        await sup.pause(status.darwin_id, reason="ui")
        await _wait_until(lambda: h.runtime.state.paused)
        await sup.resume(status.darwin_id)
        await _wait_until(lambda: not h.runtime.state.paused)
        await sup.kill(status.darwin_id, reason="testing")
        await _wait_until(lambda: not h.runtime.state.alive)

        spans = exporter.get_finished_spans()
        names = _names(spans)
        for expected in (
            "supervisor.spawn",
            "supervisor.pause",
            "supervisor.resume",
            "supervisor.kill",
        ):
            assert expected in names, names
        # Each lifecycle span is tagged with the darwin_id.
        for n in (
            "supervisor.pause",
            "supervisor.resume",
            "supervisor.kill",
        ):
            for s in _by_name(spans, n):
                assert s.attributes["darwin_id"] == status.darwin_id
    finally:
        await sup.shutdown()


async def test_quarantine_and_wallet_policy_emit_spans(exporter) -> None:
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        status = await sup.spawn(opening_usdc=60.0)
        await sup.quarantine(status.darwin_id, reason="compliance review",
                             operator="alice")
        await sup.unquarantine(status.darwin_id, operator="alice")
        await sup.set_wallet_policy(
            status.darwin_id, per_action_cap_usdc=5.0, per_day_cap_usdc=10.0
        )
        spans = exporter.get_finished_spans()
        names = _names(spans)
        for expected in (
            "supervisor.quarantine",
            "supervisor.unquarantine",
            "supervisor.set_wallet_policy",
        ):
            assert expected in names, names
        q = _by_name(spans, "supervisor.quarantine")[-1]
        assert q.attributes["reason"] == "compliance review"
        assert q.attributes["operator"] == "alice"
        pol = _by_name(spans, "supervisor.set_wallet_policy")[-1]
        assert pol.attributes["per_action_cap_usdc"] == pytest.approx(5.0)
    finally:
        await sup.shutdown()


async def test_escalation_register_emits_span(exporter) -> None:
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        status = await sup.spawn(opening_usdc=60.0, escalation_auto=None)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        h.stack.escalation.post(
            {"ticket": "T-trace", "neuron": "x", "ticket_class": "approval"}
        )
        await _wait_until(lambda: sup.router.get("T-trace") is not None)
        spans = exporter.get_finished_spans()
        # The live Darwin may register its own tickets meanwhile; pick ours.
        regs = [
            s for s in _by_name(spans, "escalation.register")
            if s.attributes["ticket_id"] == "T-trace"
        ]
        assert len(regs) == 1, _names(spans)
        s = regs[0]
        assert s.attributes["ticket_class"] == "approval"
        assert s.attributes["darwin_id"] == status.darwin_id

        # And the owner response also leaves a span.
        await sup.respond_escalation(
            status.darwin_id, escalation_id="T-trace", decision="approve"
        )
        spans = exporter.get_finished_spans()
        resps = _by_name(spans, "supervisor.respond_escalation")
        assert resps, _names(spans)
        assert resps[-1].attributes["decision"] == "approve"
    finally:
        await sup.shutdown()


async def test_monitor_alerts_emit_a_supervisor_alerts_span(exporter) -> None:
    from darwin_platform import Quotas

    quotas = Quotas(
        failure_rate_warn=0.01,
        failure_rate_critical=0.99,
        failure_rate_min_dispatches=1,
        spend_velocity_usd_per_hour_warn=1_000_000.0,
        spend_velocity_usd_per_hour_critical=1_000_000.0,
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
        status = await sup.spawn(opening_usdc=60.0)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        h.runtime.state.dispatches_done = 5
        h.runtime.state.successful_dispatches = 3
        await _wait_until(
            lambda: any(a.kind == "failure_rate"
                        for a in sup.monitor.alerts_for(status.darwin_id))
        )
        spans = exporter.get_finished_spans()
        alerts = _by_name(spans, "supervisor.alerts")
        assert alerts, _names(spans)
        assert "failure_rate" in alerts[-1].attributes["kinds"]
    finally:
        await sup.shutdown()
