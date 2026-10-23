"""E2E tier: the real ``darwin-platform serve`` process over real HTTP.

Every test here boots ``python -m darwin_platform serve`` as a subprocess on
an ephemeral port and talks to it the way a deployed client does — real TCP,
real uvicorn, real lifespan hooks, the drive loop on wall-clock time. The
integration tier covers the same routes in-process via ``ASGITransport``;
this tier proves the CLI wiring, process lifecycle, and persistence across
restarts. See doc 26.

Auto-marks every test ``e2e`` *and* ``slow`` (a subprocess boot is a few
seconds), so ``pytest -m "not slow"`` keeps them off the fast loop and
``pytest -m e2e`` / ``make test-e2e`` runs the tier on its own.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"

# Provider env prefixes stripped from the subprocess. ``import litellm`` runs
# dotenv.load_dotenv() in the test process, leaking the repo .env into
# os.environ — the subprocess would inherit it and wire REAL adapters (a real
# CDP wallet, real RPC, …), making spawn hit the network. A test that wants a
# real adapter passes its flag explicitly via ``extra_args``.
_PROVIDER_ENV_PREFIXES = (
    "DARWIN_", "CDP_", "BROWSERBASE", "BROWSER_USE", "E2B_",
    "STRIPE_", "MOONPAY_", "ONFIDO_", "TWILIO_", "CLERK_", "OTEL_",
)


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "/tests/e2e/" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)
            item.add_marker(pytest.mark.slow)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def free_port() -> int:
    """Grab an unused TCP port. There is a small TOCTOU window between
    closing the probe socket and the server's bind — acceptable for a
    single-process test run."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for(
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


class Server:
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
    def start(self) -> "Server":
        env = os.environ.copy()
        for key in list(env):
            if key.startswith(_PROVIDER_ENV_PREFIXES):
                env.pop(key, None)
        # The package is installed editable in the test venv; adding src/ to
        # PYTHONPATH makes the tier work on a bare checkout too.
        env["PYTHONPATH"] = str(_SRC) + os.pathsep + env.get("PYTHONPATH", "")
        # INFO level so the log file (temp, not stdout) carries uvicorn's
        # "Application shutdown complete" line — the evidence the lifespan
        # shutdown ran. The log only surfaces in test output on failure.
        cmd = [
            sys.executable, "-m", "darwin_platform", "serve",
            "--host", "127.0.0.1", "--port", str(self.port),
            "--log-level", "info",
            # Tests assert tick advancement within seconds; the CLI default
            # (5 s, production cadence) doesn't fit the wall-clock budget.
            "--heartbeat-seconds", "0.01",
            # Pin the shared-key wallet path; with no DARWIN_WALLET_KEY in
            # the env it falls back to FakeWallet (no network, no faucet).
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
            wait_for(self._healthy, timeout=30.0, what=f"server /healthz on :{self.port}")
        except AssertionError:
            # Surface the server log so a boot failure is debuggable.
            self.stop(sig="kill")
            raise AssertionError(f"server failed to come up. log tail:\n{self.log_tail()}")
        return self

    def stop(self, *, sig: str = "term") -> int | None:
        """Terminate the server. ``sig='term'`` is a graceful SIGTERM
        (uvicorn runs its lifespan shutdown); ``sig='kill'`` is SIGKILL
        — a hard crash with no cleanup, used by the resume tests."""
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

    def __enter__(self) -> "Server":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- introspection -----------------------------------------------------
    def _healthy(self) -> bool:
        return httpx.get(self.base_url + "/healthz", timeout=2.0).status_code == 200

    def client(self, token: str | None = None) -> httpx.Client:
        headers = {"Authorization": f"Bearer {token}"} if token else None
        return httpx.Client(base_url=self.base_url, timeout=5.0, headers=headers)

    def log_tail(self, lines: int = 40) -> str:
        try:
            text = self._log_path.read_text()
        except OSError:
            return "<no log>"
        return "\n".join(text.splitlines()[-lines:])

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc is not None else None


def assert_graceful_shutdown(srv: Server, rc: int | None) -> None:
    """Assert the server shut down cleanly under SIGTERM.

    uvicorn runs its full lifespan shutdown (which invokes
    ``Supervisor.shutdown()``) and *then* lets the process terminate via
    the SIGTERM disposition — so the exit code is ``-SIGTERM``, not 0.
    A clean shutdown is therefore: exit code 0 OR ``-SIGTERM`` (never
    ``-SIGKILL``, which :meth:`Server.stop` only produces when the
    graceful path timed out), AND the log shows the lifespan shutdown
    completing."""
    assert rc in (0, -signal.SIGTERM), (
        f"unclean exit {rc} (-SIGKILL would mean the graceful shutdown "
        f"hung); log:\n{srv.log_tail()}"
    )
    log = srv.log_tail()
    assert "Application shutdown complete" in log, f"lifespan shutdown did not complete; log:\n{log}"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def server_factory(tmp_path):
    """Yields a builder for :class:`Server` instances; every server it
    builds is torn down at the end of the test."""
    built: list[Server] = []

    def _make(
        *,
        db: str | None = None,
        port: int | None = None,
        extra_args: list[str] | None = None,
    ) -> Server:
        srv = Server(
            port=port if port is not None else free_port(),
            log_path=tmp_path / f"serve-{len(built)}.log",
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


@pytest.fixture(name="wait_for")
def _wait_for_fixture():
    """The :func:`wait_for` poller, as a fixture (conftest modules aren't
    importable under ``--import-mode=importlib``)."""
    return wait_for


@pytest.fixture(name="assert_graceful_shutdown")
def _graceful_fixture():
    return assert_graceful_shutdown


@pytest.fixture
def tokens(tmp_path) -> SimpleNamespace:
    """A ``--auth-tokens`` file in the documented shape
    (infra/docker/tokens.example.json): two owners and one operator."""
    ns = SimpleNamespace(
        alice="e2e-owner-alice",
        bob="e2e-owner-bob",
        operator="e2e-operator",
        path=tmp_path / "tokens.json",
    )
    ns.path.write_text(json.dumps([
        {"token": ns.alice, "scopes": ["owner"], "subject": "alice@example.com"},
        {"token": ns.bob, "scopes": ["owner"], "subject": "bob@example.com"},
        {"token": ns.operator, "scopes": ["platform_operator"], "subject": "ops@darwin.local"},
    ]))
    return ns
