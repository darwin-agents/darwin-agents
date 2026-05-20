"""Sandbox adapter for neuron isolation (docs/engineering/14).

Doc 14: every neuron invocation runs in its own sandboxed E2B microVM.
The genome's neuron base today doesn't run untrusted code — neurons
are part of the soma, not arbitrary user input. But the future paths
*do* exec untrusted code:

- :class:`darwin_dna.machinery.eval_harness` runs candidate mutations
  in a "clean substrate" (doc 03/10) — that substrate should be a
  fresh sandbox in production.
- Path-C neuron generation (doc 04 phase 3) creates Darwin-authored
  neurons whose first runs need extra isolation.

This module ships the :class:`SandboxClient` Protocol + an
:class:`E2BSandbox` real adapter so those paths have a real backend
when they land. The genome's existing fakes
(:class:`darwin_fakes.fake_sandbox.FakeSandbox`) are unchanged; this
adapter sits next to them.

Scope notes:

- Lifecycle is per-invocation. ``run_code`` builds + destroys the
  microVM. For long-running sessions, future expansion could expose
  ``open_session()`` / ``close_session()``; for now, keep it tight.
- Cost is debited per session — E2B bills by compute-minutes; we
  estimate via the session's reported wall time (or a configurable
  flat-rate if metering is off).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SandboxClient(Protocol):
    """Sandbox runner the platform exposes for code-executing neurons.
    ``run_code`` is intentionally minimal — one shot, return stdout +
    stderr + exit code + cost. Future expansion: streamed output,
    file sharing, network policy hooks."""

    def run_code(
        self,
        *,
        language: str,
        code: str,
        timeout_seconds: float = 30.0,
    ) -> dict:
        """Execute ``code`` inside a fresh sandbox of ``language``
        (typically ``"python"``). Returns a dict::

            {
                "ok": bool,
                "exit_code": int,
                "stdout": str,
                "stderr": str,
                "cost_usdc": float,
            }
        """
        ...


class E2BSandbox:
    """Real :class:`SandboxClient` wrapping the `e2b` Python SDK.

    Each ``run_code`` builds a fresh microVM, executes, and tears it
    down. Production deployers running many evals can swap this for a
    session-pooled variant; the Protocol stays the same.

    Args:
        wallet: :class:`WalletPort` for billing.
        sandbox_factory: zero-arg callable returning an open e2b
            sandbox (in tests a stub object with ``run_code()`` and
            ``close()`` methods). Production wires this to
            ``e2b_code_interpreter.Sandbox(api_key=os.environ[...]).
        cost_per_session: USD billed per sandbox session. E2B charges
            per compute-second; this is the platform's marked-up
            per-session pass-through. Default $0.05 mirrors a small
            ~5s run at a 10% markup over the underlying provider cost.
    """

    def __init__(
        self,
        *,
        wallet: Any,
        sandbox_factory,
        cost_per_session: float = 0.05,
    ) -> None:
        # The e2b SDK is imported by the user's ``sandbox_factory``
        # when it constructs a Sandbox — no early import check here.
        # Stub-based tests can run without the [e2b] extra installed;
        # production users see a clean ImportError at first
        # ``run_code()`` call from their factory.
        self._wallet = wallet
        self._factory = sandbox_factory
        self._cost = float(cost_per_session)

    # -- SandboxClient -----------------------------------------------------
    def run_code(
        self,
        *,
        language: str,
        code: str,
        timeout_seconds: float = 30.0,
    ) -> dict:
        # Bill first — out-of-funds Darwins don't reach E2B.
        try:
            self._wallet.debit(self._cost, f"e2b:{language}")
        except Exception as exc:
            return {
                "ok": False, "exit_code": -1, "stdout": "", "stderr": str(exc),
                "cost_usdc": 0.0,
            }

        try:
            sandbox = self._factory()
        except Exception as exc:
            return {
                "ok": False, "exit_code": -1, "stdout": "",
                "stderr": f"sandbox init failed: {exc}",
                "cost_usdc": self._cost,
            }

        try:
            result = sandbox.run_code(
                code=code,
                language=language,
                timeout=timeout_seconds,
            ) if hasattr(sandbox, "run_code") else sandbox(code)
        except Exception as exc:
            try:
                if hasattr(sandbox, "close"):
                    sandbox.close()
            except Exception:
                pass
            return {
                "ok": False, "exit_code": -1, "stdout": "", "stderr": str(exc),
                "cost_usdc": self._cost,
            }

        try:
            if hasattr(sandbox, "close"):
                sandbox.close()
        except Exception:
            pass

        return self._unpack_result(result)

    def _unpack_result(self, result: Any) -> dict:
        """Normalise the SDK's response shape. e2b's CodeInterpreter
        returns an Execution with logs, exit_code, error; tests return a
        dict with the same keys.
        """
        if isinstance(result, dict):
            ok = bool(result.get("ok", result.get("exit_code", 0) == 0))
            return {
                "ok": ok,
                "exit_code": int(result.get("exit_code", 0 if ok else 1)),
                "stdout": str(result.get("stdout", "")),
                "stderr": str(result.get("stderr", "")),
                "cost_usdc": self._cost,
            }
        exit_code = int(getattr(result, "exit_code", 0))
        ok = exit_code == 0
        # e2b's Execution has .logs.stdout/.stderr (lists) and .error.
        logs = getattr(result, "logs", None)
        if logs is not None:
            stdout = "".join(getattr(logs, "stdout", []) or [])
            stderr = "".join(getattr(logs, "stderr", []) or [])
        else:
            stdout = str(getattr(result, "stdout", "") or "")
            stderr = str(getattr(result, "stderr", "") or "")
        err = getattr(result, "error", None)
        if err is not None and not ok:
            stderr = (stderr + ("\n" if stderr else "") + str(err)).strip()
        return {
            "ok": ok,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "cost_usdc": self._cost,
        }
