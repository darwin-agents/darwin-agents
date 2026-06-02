"""Real browser substrate adapter (docs/engineering/15).

Doc 15: the platform's browser stack is **Browser-Use on Browserbase**.
Browser-Use is the agent-shaped wrapper that drives the page; Browserbase
is the headless host with stealth + region routing. The genome's
``browser.operator`` neuron consumes a tiny ``substrate.run(steps) ->
dict`` surface — same as :class:`darwin_fakes.fake_sandbox.FakeBrowser`.

This module ships:

- :class:`BrowserClient` — the Protocol the genome calls. Matches the
  fake's shape so swapping is a one-line wire change.
- :class:`BrowserUseClient` — real adapter. Wraps the ``browser_use``
  library against a Browserbase session (or any Playwright-compatible
  endpoint — Browserbase is just one of several). Handles three
  outcomes the genome cares about:
    1. Success → ``{"ok": True, "data": ..., "cost_usdc": ...}``
    2. Captcha / human-verification → ``{"blocked": "captcha", "detail":
       ..., "cost_usdc": ...}`` so ``browser.operator`` escalates
       cleanly (P4).
    3. Hard failure (page not reachable, structure changed) →
       ``{"ok": False, "detail": ..., "cost_usdc": ...}``.
- Cost is debited via the wallet *before* the run starts so an
  insufficient-funds session never reaches Browserbase. Cost matches
  Browserbase's per-session price (configurable; default $0.01 to
  mirror the FakeBrowser).

Production deployers may swap this for any Playwright-driving
implementation; the Protocol is the contract.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode


def browserbase_connect_url(
    *,
    api_key: str | None = None,
    url: str | None = None,
    session_id: str | None = None,
) -> str:
    """Build the CDP/websocket connect URL for a Browserbase session.

    browser_use connects to a remote Chrome via ``BrowserSession(cdp_url=...)``.
    Browserbase exposes that endpoint at ``wss://connect.browserbase.com``
    keyed by the API key (and an optional pre-created ``sessionId``). An
    operator may also pass a fully-formed ``url`` (e.g. a self-hosted CDP
    endpoint), which wins over the derived form.
    """
    if url:
        return url
    if not api_key:
        raise ValueError(
            "Browserbase connection requires an API key or an explicit CDP url"
        )
    params = {"apiKey": api_key}
    if session_id:
        params["sessionId"] = session_id
    return "wss://connect.browserbase.com?" + urlencode(params)


def _await_on_worker_loop(coro: Any) -> Any:
    """Drive an awaitable to completion on a fresh event loop in a dedicated
    worker thread.

    browser_use's ``Agent.run`` is async; the genome's ``browser.operator``
    neuron calls our synchronous ``run()`` from the supervisor's uvloop. Running
    the coroutine inline (``asyncio.run`` / nest_asyncio) re-enters and freezes
    that loop — the same failure mode the LiteLLM transport hit. A separate
    thread with its own loop keeps the supervisor loop free.
    """
    import threading

    box: dict[str, Any] = {}

    def _runner() -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            box["value"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001 — re-raised on join
            box["error"] = exc
        finally:
            try:
                loop.close()
            except Exception:
                pass

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


class BrowserbaseAgentRunner:
    """Synchronous ``.run(prompt)`` shim that drives a real ``browser_use``
    Agent on a Browserbase CDP session.

    This is the glue the ``browser_factory`` returns; :class:`BrowserUseClient`
    treats it as the per-session agent. It exists because the real SDK shapes
    differ from the genome's tiny surface:

    - ``Agent`` takes the task + LLM at construction (no ``browserbase_*``
      kwargs) and connects via ``browser_session`` — not a connection kwarg.
    - ``Agent.run`` is async and takes no prompt; the task is fixed at build.
    - ``Agent.run`` returns an ``AgentHistoryList``, not a success/data object.

    Dependencies are injected so this is unit-testable without the real SDK or
    a live browser: tests pass a fake ``agent_cls`` / ``browser_session_cls``
    and an identity ``awaiter``.
    """

    def __init__(
        self,
        *,
        connect_url: str,
        llm: Any,
        agent_cls: Any,
        browser_session_cls: Any,
        use_vision: bool = False,
        live_view_url: str | None = None,
        default_max_steps: int = 25,
        awaiter: Any = _await_on_worker_loop,
    ) -> None:
        self._connect_url = connect_url
        self._llm = llm
        self._agent_cls = agent_cls
        self._browser_session_cls = browser_session_cls
        self._use_vision = bool(use_vision)
        self._live_view_url = live_view_url
        self._default_max_steps = int(default_max_steps)
        self._awaiter = awaiter

    def run(self, prompt: str, *, max_steps: int | None = None) -> dict:
        session = self._browser_session_cls(cdp_url=self._connect_url)
        agent = self._agent_cls(
            task=prompt,
            llm=self._llm,
            browser_session=session,
            use_vision=self._use_vision,
        )
        history = self._awaiter(
            agent.run(max_steps=max_steps or self._default_max_steps)
        )
        return self._normalize(history)

    def _normalize(self, history: Any) -> dict:
        """AgentHistoryList → the dict shape BrowserUseClient._unpack_result
        understands. Carries the live-view URL so a captcha block can hand the
        session to the owner (doc 15 §human takeover)."""
        def _call(name: str, default: Any) -> Any:
            fn = getattr(history, name, None)
            if callable(fn):
                try:
                    return fn()
                except Exception:
                    return default
            return default

        is_successful = _call("is_successful", None)
        if is_successful is None:
            ok = bool(_call("is_done", False)) and not bool(
                _call("has_errors", False)
            )
        else:
            ok = bool(is_successful)
        errors = [e for e in (_call("errors", []) or []) if e]
        detail = "; ".join(str(e) for e in errors)
        data: dict = {
            "result": _call("final_result", None),
            "steps": _call("number_of_steps", None),
        }
        if self._live_view_url:
            data["live_view_url"] = self._live_view_url
        return {"ok": ok, "detail": detail, "data": data}


@runtime_checkable
class BrowserClient(Protocol):
    """Shape the genome's ``browser.operator`` neuron calls. The fake
    in ``darwin_fakes.fake_sandbox.FakeBrowser`` implements this; real
    adapters (this module's :class:`BrowserUseClient`) must match it
    so the swap is invisible to the neuron."""

    def run(self, steps: list[str]) -> dict:
        """Drive a browser through ``steps`` and return one of:

        - ``{"ok": True, "data": {...}, "cost_usdc": float}`` — success
        - ``{"blocked": "captcha", "detail": str, "cost_usdc": float}``
          — escalation-worthy human-verification wall
        - ``{"ok": False, "detail": str, "cost_usdc": float}`` —
          navigation_failed / structure changed / unreachable
        """
        ...


@runtime_checkable
class VisionCapableBrowserClient(BrowserClient, Protocol):
    """Optional extension — clients that support vision-mode fallback
    (doc 15 §"vision-mode fallback for canvas-only apps"). The
    CachedBrowserSubstrate consults ``supports_vision`` and falls back
    to ``run_vision`` on a DOM-exploration ``navigation_failed`` before
    escalating to the owner."""

    @property
    def supports_vision(self) -> bool: ...

    def run_vision(
        self,
        steps: list[str],
        *,
        max_steps: int = 5,
        cost_cap_usdc: float = 0.5,
    ) -> dict: ...


class BrowserUseClient:
    """Real :class:`BrowserClient` via the ``browser_use`` library on a
    Browserbase (or any Playwright-compatible) backend.

    Args:
        wallet: :class:`darwin_dna.evolution.WalletPort` for billing.
            ``debit(amount, memo)`` is called before each session so
            an over-budget Darwin never reaches Browserbase.
        browser_factory: zero-arg callable returning a configured
            ``browser_use.Agent`` (or a stub in tests). Production
            wires this to construct the Agent with the Browserbase
            connection URL + an LLM client.
        cost_per_session: USD billed to the Darwin per ``run()`` call.
            Default $0.01 matches the FakeBrowser convention.
        captcha_detectors: optional tuple of substrings checked against
            the agent's failure detail; any match flips the outcome to
            ``blocked=captcha``. Defaults to a small list of standard
            CAPTCHA service names.
    """

    _DEFAULT_CAPTCHA_DETECTORS: tuple[str, ...] = (
        "hcaptcha",
        "recaptcha",
        "captcha",
        "cloudflare turnstile",
        "verify you are human",
    )

    def __init__(
        self,
        *,
        wallet: Any,
        browser_factory,
        cost_per_session: float = 0.01,
        captcha_detectors: tuple[str, ...] | None = None,
        vision_factory: Any | None = None,
        vision_cost_per_step: float = 0.05,
    ) -> None:
        # The browser_use SDK is imported by the user's
        # ``browser_factory`` when it constructs an Agent — no early
        # import check here. That lets stub-based tests run without
        # the [browser] extra installed; production users see a
        # clean ImportError at first ``run()`` from their factory.
        self._wallet = wallet
        self._browser_factory = browser_factory
        self._cost = float(cost_per_session)
        self._captcha_detectors = tuple(
            (captcha_detectors or self._DEFAULT_CAPTCHA_DETECTORS)
        )
        # Optional vision-mode factory. Doc 15: "Browser-Use vision mode on
        # the SAME Browserbase session for canvas-only apps / hard anti-bot
        # walls." Pass a zero-arg callable that returns a vision-enabled
        # browser_use.Agent (or compatible). When unset, the client reports
        # supports_vision=False and the cached substrate will not try the
        # fallback.
        self._vision_factory = vision_factory
        self._vision_cost_per_step = float(vision_cost_per_step)

    # -- BrowserClient -----------------------------------------------------
    def run(self, steps: list[str]) -> dict:
        # Bill first; if the wallet refuses (out of funds, cap), report
        # the same way the FakeBrowser does so the genome's branch logic
        # is uniform across backends.
        try:
            self._wallet.debit(self._cost, "browserbase:session")
        except Exception as exc:
            return {"ok": False, "detail": f"no funds: {exc}",
                    "cost_usdc": 0.0}

        # Construct one Agent per session (browser-use's Agent is
        # session-bound). The factory abstracts Browserbase wiring so
        # tests can return a stub.
        try:
            agent = self._browser_factory()
        except Exception as exc:
            return {"ok": False, "detail": f"agent init failed: {exc}",
                    "cost_usdc": self._cost}

        prompt = " ; ".join(steps) if steps else ""
        try:
            result = agent.run(prompt) if hasattr(agent, "run") else agent(prompt)
        except Exception as exc:
            detail = str(exc)
            if self._is_captcha(detail):
                return {"blocked": "captcha", "detail": detail,
                        "cost_usdc": self._cost}
            return {"ok": False, "detail": detail, "cost_usdc": self._cost}

        # ``result`` can be a string, a dict, or a browser_use-specific
        # result object. We probe in the order the library evolves:
        # 1. result.success / result.error_message (current shape)
        # 2. result["ok"] / result["error"] (test-stub dict)
        # 3. str(result) success default.
        success, detail, data = self._unpack_result(result)
        # A live-view URL (Browserbase session debug view) lets the owner take
        # over the live session when the run hits a captcha / human wall
        # (doc 15 §human takeover). Thread it through the escalation outcomes.
        live = data.get("live_view_url") if isinstance(data, dict) else None
        if not success:
            if self._is_captcha(detail):
                out = {"blocked": "captcha", "detail": detail,
                       "cost_usdc": self._cost}
                if live:
                    out["live_view_url"] = live
                return out
            out = {"ok": False, "detail": detail, "cost_usdc": self._cost}
            if live:
                out["live_view_url"] = live
            return out
        return {"ok": True, "data": data, "cost_usdc": self._cost}

    # -- vision-mode fallback (doc 15) -----------------------------------
    @property
    def supports_vision(self) -> bool:
        return self._vision_factory is not None

    def run_vision(
        self,
        steps: list[str],
        *,
        max_steps: int = 5,
        cost_cap_usdc: float = 0.5,
    ) -> dict:
        """Vision-mode fallback. Used by :class:`CachedBrowserSubstrate`
        on a ``navigation_failed`` from DOM exploration. Hard cost cap
        + step cap per doc 15; the call refuses before launching the
        session if those caps couldn't possibly be met.

        Vision is *expensive* — each step incurs an LLM screenshot call.
        The neuron sees the same three outcomes (ok / blocked / fail)
        but the result dict carries ``via='vision'`` so the dashboard
        can show the breakdown."""
        if self._vision_factory is None:
            return {
                "ok": False,
                "detail": "vision-mode not configured",
                "cost_usdc": 0.0,
                "via": "vision",
            }
        if cost_cap_usdc <= 0 or max_steps <= 0:
            return {
                "ok": False,
                "detail": "vision-mode caps misconfigured",
                "cost_usdc": 0.0,
                "via": "vision",
            }
        # Estimate worst-case cost; bill upfront so the wallet can refuse
        # before we contact Browserbase.
        worst_case_cost = min(
            cost_cap_usdc, max_steps * self._vision_cost_per_step,
        )
        try:
            self._wallet.debit(worst_case_cost, "browserbase:vision_session")
        except Exception as exc:
            return {
                "ok": False, "detail": f"no funds: {exc}",
                "cost_usdc": 0.0, "via": "vision",
            }
        try:
            agent = self._vision_factory()
        except Exception as exc:
            return {
                "ok": False, "detail": f"vision agent init failed: {exc}",
                "cost_usdc": worst_case_cost, "via": "vision",
            }
        prompt = " ; ".join(steps) if steps else ""
        try:
            # Browser-Use's vision-mode `run` accepts `use_vision=True` or
            # the agent is constructed with it. The factory owns that
            # detail — we just call run().
            result = (
                agent.run(prompt, max_steps=max_steps)
                if hasattr(agent, "run") else agent(prompt)
            )
        except Exception as exc:
            detail = str(exc)
            if self._is_captcha(detail):
                return {
                    "blocked": "captcha", "detail": detail,
                    "cost_usdc": worst_case_cost, "via": "vision",
                }
            return {
                "ok": False, "detail": detail,
                "cost_usdc": worst_case_cost, "via": "vision",
            }
        success, detail, data = self._unpack_result(result)
        if not success:
            if self._is_captcha(detail):
                return {
                    "blocked": "captcha", "detail": detail,
                    "cost_usdc": worst_case_cost, "via": "vision",
                }
            return {
                "ok": False, "detail": detail,
                "cost_usdc": worst_case_cost, "via": "vision",
            }
        return {
            "ok": True, "data": data,
            "cost_usdc": worst_case_cost, "via": "vision",
        }

    # -- internals --------------------------------------------------------
    def _is_captcha(self, detail: str | None) -> bool:
        if not detail:
            return False
        low = detail.lower()
        return any(d in low for d in self._captcha_detectors)

    @staticmethod
    def _unpack_result(result: Any) -> tuple[bool, str, dict]:
        # Object shape (browser_use's AgentResult-ish).
        ok = getattr(result, "success", None)
        if ok is not None:
            return bool(ok), str(
                getattr(result, "error_message", "") or ""
            ), dict(getattr(result, "data", {}) or {})
        # Dict shape (used by tests + simple wrappers).
        if isinstance(result, dict):
            if "ok" in result:
                return bool(result["ok"]), str(result.get("detail", "")), dict(
                    result.get("data", {})
                )
        # String or anything else: treat as a successful free-text
        # outcome, surface the string as data.
        return True, "", {"text": str(result)}
