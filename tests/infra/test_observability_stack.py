"""The observability stack wires together (doc 22): Prometheus →
Alertmanager, the OTel collector receives the supervisor's spans and exports
to Jaeger, and every metric an alert rule or Grafana panel queries is one
the supervisor's ``/metrics`` endpoint actually emits.

Config-shape tests (yaml.safe_load / json) plus one in-process render of
``/metrics`` — no docker required. They guard against the dashboards,
alerts, and the code silently drifting apart.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_REPO = Path(__file__).resolve().parents[2]
_DOCKER = _REPO / "infra" / "docker"
_DASHBOARD = _REPO / "infra" / "grafana" / "darwin-platform-dashboard.json"


def _load(rel: str):
    return yaml.safe_load((_DOCKER / rel).read_text())


def test_prometheus_routes_to_alertmanager_and_loads_rules():
    prom = _load("prometheus.yml")
    assert "/etc/prometheus/alerting_rules.yml" in prom["rule_files"]
    targets = prom["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"]
    assert "alertmanager:9093" in targets


def test_prometheus_scrapes_supervisor_litellm_and_collector():
    jobs = {j["job_name"] for j in _load("prometheus.yml")["scrape_configs"]}
    assert {"darwin-supervisor", "darwin-litellm", "otel-collector"} <= jobs


def test_alert_rules_target_real_exposed_metrics():
    rules = _load("alerting_rules.yml")
    exprs = " ".join(
        r["expr"]
        for g in rules["groups"] for r in g["rules"]
    )
    # Every metric referenced must be one the supervisor actually exposes
    # (or a standard scrape metric). Guards against a rule on a typo metric.
    for metric in (
        "darwin_platform_alerts_total",
        "darwin_platform_throttle_installed",
        "darwin_platform_audit_entries_total",
        "darwin_platform_escalations_open",
        "darwin_platform_sanctions_refusals_total",
        "darwin_platform_darwins",
    ):
        assert metric in exprs, f"{metric} not referenced — check rule wiring"


def test_otel_collector_receives_otlp_http_on_4318():
    cfg = _load("otel-collector-config.yaml")
    http = cfg["receivers"]["otlp"]["protocols"]["http"]["endpoint"]
    assert http.endswith(":4318")
    assert "traces" in cfg["service"]["pipelines"]


def test_otel_collector_exports_traces_to_jaeger():
    cfg = _load("otel-collector-config.yaml")
    # A Jaeger OTLP exporter exists and is wired into the traces pipeline.
    assert cfg["exporters"]["otlp/jaeger"]["endpoint"] == "jaeger:4317"
    assert "otlp/jaeger" in cfg["service"]["pipelines"]["traces"]["exporters"]


def test_compose_runs_jaeger_backend():
    svc = _load("docker-compose.yml")["services"]
    assert "jaeger" in svc
    # The collector depends on the tracing backend it exports to.
    assert "jaeger" in svc["otel-collector"].get("depends_on", [])
    assert svc["jaeger"]["environment"]["COLLECTOR_OTLP_ENABLED"] == "true"


def test_alertmanager_has_a_critical_route():
    am = _load("alertmanager.yml")
    routes = am["route"].get("routes", [])
    assert any(
        any(m.startswith("severity") and "critical" in m for m in r.get("matchers", []))
        for r in routes
    ), "expected a dedicated critical route"


def test_compose_runs_collector_and_alertmanager_and_points_supervisor_at_otlp():
    compose = _load("docker-compose.yml")
    svc = compose["services"]
    assert "otel-collector" in svc and "alertmanager" in svc
    # The supervisor exports spans to the collector.
    cmd = svc["supervisor"]["command"]
    assert "--otlp-endpoint" in cmd
    assert any("otel-collector:4318" in str(c) for c in cmd)
    # Prometheus mounts the rules + alertmanager mounts its config.
    assert any("alerting_rules.yml" in v for v in svc["prometheus"]["volumes"])
    assert any("alertmanager.yml" in v for v in svc["alertmanager"]["volumes"])


# -- metrics contract: queries ↔ the code ------------------------------------
@pytest.fixture(scope="module")
def emitted_metrics() -> set[str]:
    """Metric families the supervisor exposes with one live Darwin, read
    from the Prometheus ``# TYPE`` lines of a real ``/metrics`` render."""
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    import asyncio

    from darwin_platform import Supervisor
    from darwin_platform.api import build_app

    async def render() -> str:
        sup = Supervisor(heartbeat_seconds=0.0)
        transport = httpx.ASGITransport(app=build_app(sup))
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                await c.post("/darwins", json={"opening_usdc": 60.0})
                return (await c.get("/metrics")).text
        finally:
            await sup.shutdown()

    text = asyncio.run(render())
    return set(re.findall(r"^# TYPE (\S+)", text, flags=re.MULTILINE))


def _referenced(text: str) -> set[str]:
    return set(re.findall(r"darwin_platform_[a-z_]+", text))


def test_alert_rules_only_query_emitted_metrics(emitted_metrics):
    referenced = _referenced((_DOCKER / "alerting_rules.yml").read_text())
    assert referenced, "alert rules should query platform metrics"
    assert referenced <= emitted_metrics, f"alerts on unknown metrics: {referenced - emitted_metrics}"


def test_grafana_dashboard_only_queries_emitted_metrics(emitted_metrics):
    dashboard = json.loads(_DASHBOARD.read_text())
    assert dashboard.get("panels"), "dashboard has no panels"
    referenced = _referenced(_DASHBOARD.read_text())
    assert referenced <= emitted_metrics, f"panels on unknown metrics: {referenced - emitted_metrics}"


def test_every_alert_rule_is_actionable():
    for group in _load("alerting_rules.yml")["groups"]:
        for rule in group["rules"]:
            name = rule.get("alert", "<recording rule>")
            assert rule.get("labels", {}).get("severity") in {"info", "warning", "critical"}, name
            assert rule.get("annotations", {}).get("summary"), f"{name} has no summary annotation"
