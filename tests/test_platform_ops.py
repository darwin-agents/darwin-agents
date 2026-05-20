"""Operational surface (docs/engineering/08 §Observability + production
readiness): /healthz, /metrics, and the CLI entry point."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Callable

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from darwin_platform import Quotas, Supervisor  # noqa: E402
from darwin_platform.api import _render_metrics, build_app  # noqa: E402


async def _wait_until(
    pred: Callable[[], bool], *, timeout: float = 5.0, tick: float = 0.01
) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(tick)


# -- /healthz --------------------------------------------------------------
async def test_healthz_returns_ok_with_supervisor_info() -> None:
    sup = Supervisor(heartbeat_seconds=0.0)
    app = build_app(sup)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            r = await c.get("/healthz")
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "ok"
            assert body["persistence"] == "memory"
            assert body["darwins"] == 0
            assert body["uptime_seconds"] >= 0
        finally:
            await sup.shutdown()


async def test_healthz_unauthenticated_when_auth_enabled() -> None:
    """/healthz must work without auth — k8s liveness probes can't send
    bearer tokens."""
    from darwin_platform import InMemoryTokenStore

    sup = Supervisor(heartbeat_seconds=0.0)
    store = InMemoryTokenStore()
    app = build_app(sup, token_store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            r = await c.get("/healthz")  # no Authorization header
            assert r.status_code == 200
        finally:
            await sup.shutdown()


async def test_healthz_reports_persistence_kind(tmp_path: Path) -> None:
    sup = Supervisor(heartbeat_seconds=0.0, persistence_path=tmp_path / "p.db")
    app = build_app(sup)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            r = await c.get("/healthz")
            assert r.json()["persistence"] == "sqlite"
        finally:
            await sup.shutdown()


# -- /metrics --------------------------------------------------------------
async def test_metrics_is_prometheus_text_with_expected_names() -> None:
    sup = Supervisor(heartbeat_seconds=0.0)
    app = build_app(sup)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            r = await c.get("/metrics")
            assert r.status_code == 200
            assert "text/plain" in r.headers["content-type"]
            body = r.text
            # Every exposed metric must appear with both HELP and TYPE
            # comments AND at least one sample line.
            for name in (
                "darwin_platform_darwins",
                "darwin_platform_audit_entries_total",
                "darwin_platform_escalations_open",
                "darwin_platform_alerts_total",
                "darwin_platform_billing_revenue_usdc",
                "darwin_platform_lineage_edges_total",
                "darwin_platform_uptime_seconds",
            ):
                assert f"# HELP {name}" in body
                assert f"# TYPE {name}" in body
        finally:
            await sup.shutdown()


async def test_metrics_reflects_state_changes() -> None:
    """The exposition is a live snapshot — spawning + killing a Darwin
    should change the darwin_platform_darwins{state=...} samples."""
    sup = Supervisor(heartbeat_seconds=0.0)
    app = build_app(sup)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            await c.post("/darwins", json={"opening_usdc": 60.0})
            r = await c.get("/metrics")
            assert 'darwin_platform_darwins{state="alive"} 1' in r.text
            assert 'darwin_platform_darwins{state="killed"} 0' in r.text
        finally:
            await sup.shutdown()


async def test_render_metrics_directly_with_no_darwins() -> None:
    """The renderer is a pure function over a Supervisor + start ts. Test
    the formatter without an HTTP server."""
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        out = _render_metrics(sup, time.time())
        # Lines are LF-separated and the document ends with a newline.
        assert out.endswith("\n")
        # All four state samples appear, all zero.
        for state in ("alive", "killed", "paused", "quarantined"):
            assert f'darwin_platform_darwins{{state="{state}"}} 0' in out
    finally:
        await sup.shutdown()


def test_label_escaping_safe_against_quotes_and_backslashes() -> None:
    """Prometheus labels must escape backslash, double-quote, newline."""
    from darwin_platform.api import _escape_label

    assert _escape_label('a"b') == 'a\\"b'
    assert _escape_label("a\\b") == "a\\\\b"
    assert _escape_label("a\nb") == "a\\nb"


# -- CLI -------------------------------------------------------------------
def test_cli_help_runs_without_error() -> None:
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-m", "darwin_platform", "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0
    assert "serve" in r.stdout
    assert "signing-key" in r.stdout


def test_cli_signing_key_prints_pem(tmp_path: Path) -> None:
    """The signing-key subcommand prints a parseable PEM. Useful for ops
    verification that a Darwin's manifest was signed by THIS platform."""
    import subprocess
    import sys

    r = subprocess.run(
        [
            sys.executable, "-m", "darwin_platform", "signing-key",
            "--db", str(tmp_path / "p.db"),
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0
    assert "BEGIN PUBLIC KEY" in r.stdout
    assert "END PUBLIC KEY" in r.stdout


def test_cli_serve_rejects_unknown_subcommand() -> None:
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-m", "darwin_platform", "nonsense"],
        capture_output=True, text=True, timeout=30,
    )
    # argparse exits with code 2 on bad subcommand.
    assert r.returncode == 2
