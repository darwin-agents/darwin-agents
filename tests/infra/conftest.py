"""Infra tier: the deployment assets are correct before anything deploys.

Static, offline checks over ``infra/`` (Dockerfile, Compose, Helm chart,
Terraform, Prometheus/Alertmanager/OTel/Grafana config), the per-Darwin
Kubernetes manifests the supervisor generates, the GitHub workflows, and the
configuration reference — the drift that otherwise only shows up at deploy
time. Checks that need a CLI (``helm``, ``terraform``, ``docker compose``)
run it when it's on PATH and skip otherwise; CI installs them. See doc 26.

Auto-marks every test ``infra``; the tier is fast and runs on every push.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
INFRA = REPO / "infra"


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "/tests/infra/" in str(item.fspath):
            item.add_marker(pytest.mark.infra)


@pytest.fixture(scope="session")
def repo() -> Path:
    return REPO


@pytest.fixture(scope="session")
def load_yaml():
    def _load(rel: str):
        return yaml.safe_load((REPO / rel).read_text(encoding="utf-8"))

    return _load


@pytest.fixture(scope="session")
def run_tool():
    """Run a CLI from the repo root, skipping the test when it isn't installed."""

    def _run(*cmd: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
        if shutil.which(cmd[0]) is None:
            pytest.skip(f"{cmd[0]} not on PATH")
        return subprocess.run(
            cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout, check=False,
        )

    return _run
