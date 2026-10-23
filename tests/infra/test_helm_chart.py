"""The supervisor Helm chart (infra/charts/darwin-platform) is well-formed,
hardened by default, and every template value resolves.

The ``helm`` checks mirror .github/workflows/helm.yml so the same gate runs
locally; they skip when helm isn't installed."""

from __future__ import annotations

import re

import pytest

CHART = "infra/charts/darwin-platform"
# Conventional overrides the chart reads but deliberately leaves unset.
OPTIONAL_VALUES = {"nameOverride", "fullnameOverride", "podDisruptionBudget.maxUnavailable"}


@pytest.fixture(scope="module")
def values(load_yaml):
    return load_yaml(f"{CHART}/values.yaml")


def _lookup(tree, dotted: str) -> bool:
    node = tree
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def test_chart_metadata(load_yaml):
    chart = load_yaml(f"{CHART}/Chart.yaml")
    assert chart["apiVersion"] == "v2"
    assert chart["name"] == "darwin-platform"
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(chart["version"])), "chart version must be semver"
    assert chart.get("appVersion"), "appVersion pins the image the chart is validated against"


def test_every_template_value_reference_resolves(repo, values):
    unresolved: dict[str, set[str]] = {}
    for template in (repo / CHART / "templates").iterdir():
        for ref in re.findall(r"\.Values\.([A-Za-z0-9_.]+)", template.read_text()):
            if ref not in OPTIONAL_VALUES and not _lookup(values, ref):
                unresolved.setdefault(ref, set()).add(template.name)
    assert not unresolved, f"templates reference values missing from values.yaml: {unresolved}"


def test_defaults_are_hardened(values):
    assert values["image"]["repository"].startswith("ghcr.io/")
    limits = values["resources"]["limits"]
    assert limits.get("cpu") and limits.get("memory"), "default resource limits required"
    assert values["podSecurityContext"]["runAsNonRoot"] is True, "must not run as root"
    container_sc = values["containerSecurityContext"]
    assert container_sc["allowPrivilegeEscalation"] is False
    assert container_sc["capabilities"]["drop"] == ["ALL"]
    assert values["networkPolicy"]["enabled"] is True, "default-deny egress is on by default"


def test_production_fixture_turns_on_auth_tls_and_monitoring(load_yaml):
    prod = load_yaml(f"{CHART}/tests/values-prod.yaml")
    assert prod["auth"]["enabled"] is True and prod["auth"].get("existingSecret")
    assert prod["ingress"]["enabled"] is True and prod["ingress"].get("tls")
    assert prod["serviceMonitor"]["enabled"] is True


def test_helm_lint(run_tool):
    proc = run_tool("helm", "lint", CHART)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize(
    ("extra", "kinds"),
    [
        ([], {"Deployment", "Service", "PersistentVolumeClaim", "ServiceAccount", "NetworkPolicy"}),
        (["-f", f"{CHART}/tests/values-prod.yaml"], {"Deployment", "Ingress", "ServiceMonitor"}),
    ],
    ids=["default", "prod"],
)
def test_helm_template_renders_expected_kinds(run_tool, extra, kinds):
    proc = run_tool("helm", "template", "darwin", CHART, *extra)
    assert proc.returncode == 0, proc.stderr
    rendered = set(re.findall(r"^kind: (\w+)$", proc.stdout, flags=re.MULTILINE))
    assert kinds <= rendered, f"missing kinds: {kinds - rendered}"
