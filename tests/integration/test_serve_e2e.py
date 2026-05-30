"""End-to-end smoke test: the real ``darwin-platform serve`` process.

Every other platform-API test drives the FastAPI app in-process via
``httpx.ASGITransport`` — fast, but it never exercises the CLI argument
parsing, the uvicorn server, the lifespan startup/shutdown hooks, or the
drive loop running over real wall-clock time in a separate process.

This test closes that gap. It boots ``python -m darwin_platform serve``
as a subprocess on an ephemeral port, drives a Darwin over real HTTP,
and asserts a clean shutdown. It also covers the doc-20 "kill -9 the
supervisor, restart" criterion: a Darwin spawned against a SQLite
``--db`` resumes after the process is SIGKILLed and rebooted.

Marked ``slow`` (subprocess boot + real polling, a few seconds each) so
``pytest -m 'not slow'`` skips it on the fast loop; CI runs it in the
full suite.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.slow

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _free_port() -> int:
    """Grab an unused TCP port. There is a small TOCTOU window between
    closing the probe socket and the server's bind — acceptable for a
    single-process test run."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for(
    predicate,
    *,
    timeout: float = 30.0,
    tick: float = 0.1,
    what: str = "condition",
) -> None:
    """Poll ``predicate`` until it returns truthy or the deadline passes.
    Exceptions from the predicate (e.g. connection-refused while the
    server is still booting) are swallowed and retried."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as exc:  # noqa: BLE001 — booting server, retry
            last_exc = exc
        time.sleep(tick)
    suffix = f" (last error: {last_exc})" if last_exc is not None else ""
    raise AssertionError(f"timed out after {timeout}s waiting for {what}{suffix}")


class _Server:
    """A ``darwin-platform serve`` subprocess, context-managed so the
    process is always reaped — even when an assertion fails mid-test."""

    def __init__(
        self,
        *,
        port: int,
        log_path: Path,
        db: str | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self.port = port
        self.db = db
        self.extra_args = list(extra_args or [])
        self.base_url = f"http://127.0.0.1:{port}"
        self._log_path = log_path
        self._proc: subprocess.Popen | None = None
        self._logf = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "_Server":
        env = os.environ.copy()
        # Hermetic: ``import litellm`` runs dotenv.load_dotenv() in this
        # process, leaking the repo .env into os.environ — which the
        # subprocess would inherit and use to wire REAL adapters (a real CDP
        # wallet, real RPC, etc.), breaking these fakes-only assertions and
        # making spawn hit the network. Strip every provider var so the e2e
        # serve runs on fakes; a test that wants a real adapter passes its
        # flag explicitly via extra_args.
        for key in list(env):
            if key.startswith((
                "DARWIN_", "CDP_", "BROWSERBASE", "BROWSER_USE", "E2B_",
                "STRIPE_", "MOONPAY_", "ONFIDO_", "TWILIO_", "CLERK_", "OTEL_",
            )):
                env.pop(key, None)
        # Belt-and-suspenders: the package is installed editable in the
        # test venv, but adding src/ to PYTHONPATH makes the test work on
        # a bare checkout too.
        env["PYTHONPATH"] = str(_SRC) + os.pathsep + env.get("PYTHONPATH", "")
        # INFO level so the log file (temp, not stdout) carries uvicorn's
        # "Application shutdown complete" line — the evidence the lifespan
        # shutdown ran. The log only surfaces in test output on failure.
        cmd = [
            sys.executable, "-m", "darwin_platform", "serve",
            "--host", "127.0.0.1", "--port", str(self.port),
            "--log-level", "info",
            # E2E tests assert tick advancement within ~15 s. The CLI
            # default is 5 s (production cadence), which doesn't fit.
            # Override to a fast cadence so the lifespan + drive loop
            # exercise themselves without burning the wall-clock budget.
            "--heartbeat-seconds", "0.01",
            # The CLI's --wallet-provider default is "cdp" which requires
            # CDP credentials and sys.exit(2)s without them. The e2e tests
            # don't exercise wallet behaviour, so pin to the shared-key
            # path; with no DARWIN_WALLET_KEY env it falls back to FakeWallet.
            "--wallet-provider", "web3",
        ]
        if self.db is not None:
            cmd += ["--db", self.db]
        cmd += self.extra_args
        self._logf = open(self._log_path, "w")
        self._proc = subprocess.Popen(
            cmd, cwd=str(_REPO_ROOT), env=env,
            stdout=self._logf, stderr=subprocess.STDOUT,
        )
        try:
            _wait_for(
                self._healthy, timeout=30.0,
                what=f"server /healthz on :{self.port}",
            )
        except AssertionError:
            # Surface the server log so a boot failure is debuggable.
            self.stop(sig="kill")
            raise AssertionError(
                f"server failed to come up. log tail:\n{self.log_tail()}"
            )
        return self

    def stop(self, *, sig: str = "term") -> int | None:
        """Terminate the server. ``sig='term'`` is a graceful SIGTERM
        (uvicorn runs its lifespan shutdown); ``sig='kill'`` is SIGKILL
        — a hard crash with no cleanup, used for the resume test."""
        if self._proc is None:
            return None
        if self._proc.poll() is None:
            if sig == "kill":
                self._proc.kill()
            else:
                self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
        rc = self._proc.returncode
        if self._logf is not None:
            self._logf.close()
            self._logf = None
        return rc

    def __enter__(self) -> "_Server":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- introspection -----------------------------------------------------
    def _healthy(self) -> bool:
        r = httpx.get(self.base_url + "/healthz", timeout=2.0)
        return r.status_code == 200

    def log_tail(self, lines: int = 40) -> str:
        try:
            text = self._log_path.read_text()
        except OSError:
            return "<no log>"
        return "\n".join(text.splitlines()[-lines:])

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc is not None else None


def _assert_graceful_shutdown(srv: "_Server", rc: int | None) -> None:
    """Assert the server shut down cleanly under SIGTERM.

    uvicorn runs its full lifespan shutdown (which invokes
    ``Supervisor.shutdown()``) and *then* lets the process terminate via
    the SIGTERM disposition — so the exit code is ``-SIGTERM``, not 0.
    A clean shutdown is therefore: exit code 0 OR ``-SIGTERM`` (never
    ``-SIGKILL``, which :meth:`_Server.stop` only produces when the
    graceful path timed out), AND the log shows the lifespan shutdown
    completing."""
    assert rc in (0, -signal.SIGTERM), (
        f"unclean exit {rc} (─SIGKILL would mean the graceful shutdown "
        f"hung); log:\n{srv.log_tail()}"
    )
    log = srv.log_tail()
    assert "Application shutdown complete" in log, (
        f"lifespan shutdown did not complete; log:\n{log}"
    )


@pytest.fixture
def server_factory(tmp_path):
    """Yields a builder for :class:`_Server` instances; every server it
    builds is torn down at the end of the test."""
    built: list[_Server] = []

    def _make(
        *,
        db: str | None = None,
        port: int | None = None,
        extra_args: list[str] | None = None,
    ) -> _Server:
        idx = len(built)
        srv = _Server(
            port=port if port is not None else _free_port(),
            log_path=tmp_path / f"serve-{idx}.log",
            db=db,
            extra_args=extra_args,
        )
        built.append(srv)
        return srv

    try:
        yield _make
    finally:
        for srv in built:
            try:
                srv.stop()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass


# ---------------------------------------------------------------------------
# test 1 — spawn → run → kill against the live HTTP server
# ---------------------------------------------------------------------------
def test_serve_spawns_runs_and_kills_a_darwin(server_factory):
    srv = server_factory().start()
    client = httpx.Client(base_url=srv.base_url, timeout=5.0)

    # healthz reports the in-memory persistence backend.
    health = client.get("/healthz").json()
    assert health["status"] == "ok"
    assert health["persistence"] == "memory"

    # spawn.
    r = client.post("/darwins", json={"opening_usdc": 100.0})
    assert r.status_code == 201, r.text
    spawned = r.json()
    did = spawned["darwin_id"]
    assert spawned["alive"] is True
    assert spawned["balance_usdc"] == pytest.approx(100.0)

    # the drive loop is a real asyncio task in the subprocess — its tick
    # counter must advance over wall-clock time without us poking it.
    def _ticked() -> bool:
        return client.get(f"/darwins/{did}").json()["tick"] > 5

    _wait_for(_ticked, timeout=15.0, what="darwin tick to advance")

    # the audit chain is growing + hash-chained (first record is birth).
    events = client.get(f"/darwins/{did}/events?limit=3").json()
    assert events["total"] > 0
    assert events["events"][0]["kind"] == "event:birth"
    assert events["events"][0]["prev_hash"] == "0" * 64

    # metrics endpoint serves Prometheus text with the fleet gauge.
    metrics = client.get("/metrics").text
    assert 'darwin_platform_darwins{state="alive"} 1' in metrics

    # kill — the genome must observe the flag and die.
    assert client.post(f"/darwins/{did}/kill").status_code == 200

    def _dead() -> bool:
        return client.get(f"/darwins/{did}").json()["alive"] is False

    _wait_for(_dead, timeout=10.0, what="darwin to die after kill")
    body = client.get(f"/darwins/{did}").json()
    assert body["killed"] is True

    client.close()

    # graceful SIGTERM → uvicorn runs the lifespan shutdown (which calls
    # Supervisor.shutdown()) → process exits via the signal disposition.
    rc = srv.stop(sig="term")
    _assert_graceful_shutdown(srv, rc)


# ---------------------------------------------------------------------------
# test 2 — doc-20 "kill -9 the supervisor, restart" resume criterion
# ---------------------------------------------------------------------------
def test_serve_resumes_darwin_after_kill_9(server_factory, tmp_path):
    db = str(tmp_path / "state.db")

    # boot 1: spawn a Darwin against SQLite persistence.
    srv1 = server_factory(db=db).start()
    c1 = httpx.Client(base_url=srv1.base_url, timeout=5.0)
    assert c1.get("/healthz").json()["persistence"] == "sqlite"

    spawned = c1.post("/darwins", json={"opening_usdc": 100.0}).json()
    did = spawned["darwin_id"]

    # let it run a little so the lifecycle row + a checkpoint are durable.
    _wait_for(
        lambda: c1.get(f"/darwins/{did}").json()["tick"] > 5,
        timeout=15.0, what="darwin tick before crash",
    )
    events_before = c1.get(f"/darwins/{did}/events?limit=1").json()["total"]
    c1.close()

    # SIGKILL — a hard crash. No lifespan shutdown, no clean teardown.
    srv1.stop(sig="kill")

    # boot 2: a fresh process on a fresh port, same SQLite file. The
    # supervisor's _resume_from_persistence runs inside the lifespan
    # startup, so by the time /healthz answers the Darwin is back.
    srv2 = server_factory(db=db).start()
    c2 = httpx.Client(base_url=srv2.base_url, timeout=5.0)

    darwins = c2.get("/darwins").json()["darwins"]
    assert did in darwins, (
        f"darwin {did} did not resume after restart; "
        f"fleet={darwins}; log:\n{srv2.log_tail()}"
    )

    status = c2.get(f"/darwins/{did}").json()
    assert status["alive"] is True
    assert status["killed"] is False

    # the audit chain survived the crash — it carries at least what was
    # written before the kill, and the chain still verifies (the genome's
    # AuditLogger re-verifies from durable storage at boot; a broken chain
    # would have aborted the resume).
    events_after = c2.get(f"/darwins/{did}/events?limit=1").json()["total"]
    assert events_after >= events_before

    # and it keeps running — the resumed drive loop advances the tick.
    tick_now = c2.get(f"/darwins/{did}").json()["tick"]
    _wait_for(
        lambda: c2.get(f"/darwins/{did}").json()["tick"] > tick_now,
        timeout=15.0, what="resumed darwin to keep ticking",
    )

    c2.close()
    rc = srv2.stop(sig="term")
    _assert_graceful_shutdown(srv2, rc)


# ---------------------------------------------------------------------------
# test 3 — Phase 1/2 wiring flags are accepted by the real serve process
# ---------------------------------------------------------------------------
def test_serve_accepts_wiring_flags(server_factory):
    """Boots the real process with the OTLP + Phase-2 wiring flags and
    confirms it comes up healthy and the OTLP exporter is installed.

    The OTLP install sets a process-global TracerProvider, so it can
    only be exercised out-of-process like this; the endpoint can be
    unreachable (the BatchSpanProcessor exports in the background and
    swallows connection errors) — what we assert is that `serve`
    accepted the flag and logged the exporter wiring."""
    srv = server_factory(
        extra_args=[
            "--otlp-endpoint", "http://127.0.0.1:4318",
            "--enable-drift-detection",
            "--enable-merkle-anchor",
            "--kyc-provider", "stub",
            "--maintenance-interval", "2",
        ]
    ).start()
    client = httpx.Client(base_url=srv.base_url, timeout=5.0)

    # Came up healthy with all the wiring flags applied.
    assert client.get("/healthz").json()["status"] == "ok"

    # A Darwin still spawns + runs with the Phase-2 services wired.
    did = client.post("/darwins", json={"opening_usdc": 100.0}).json()[
        "darwin_id"
    ]
    _wait_for(
        lambda: client.get(f"/darwins/{did}").json()["tick"] > 5,
        timeout=15.0, what="darwin tick under wiring flags",
    )
    client.close()

    rc = srv.stop(sig="term")
    _assert_graceful_shutdown(srv, rc)

    # The OTLP exporter wiring logged its startup line.
    log = srv.log_tail(lines=200)
    assert "tracing: exporting spans to" in log, (
        f"OTLP wiring did not log; log:\n{log}"
    )
