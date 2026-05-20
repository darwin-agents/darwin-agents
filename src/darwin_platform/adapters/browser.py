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
        if not success:
            if self._is_captcha(detail):
                return {"blocked": "captcha", "detail": detail,
                        "cost_usdc": self._cost}
            return {"ok": False, "detail": detail, "cost_usdc": self._cost}
        return {"ok": True, "data": data, "cost_usdc": self._cost}

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
