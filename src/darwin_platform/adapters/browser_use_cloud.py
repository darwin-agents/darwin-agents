"""Browser Use Cloud V3 Agent adapter (docs/engineering/15, 31).

Production-grade :class:`BrowserClient` backed by the **Browser Use
Cloud V3 Agent API** — the cloud orchestrates the browser (Stealth
Browsers runtime, CAPTCHA, residential proxies) *and* the agent loop;
the platform sends a natural-language task and receives a result. This
is the alternative to the existing :class:`BrowserUseClient` path that
runs the OSS ``browser_use`` library in-process against a Browserbase
runtime.

When to pick this over ``BrowserUseClient`` is a deployment-time
decision — both Implement the same Protocol so a Darwin's neuron code
doesn't care which is wired. See ``docs/engineering/31-browser-use-evaluation.md``
for the pricing + risk write-up. The CLI flag is
``--browser-provider {browserbase,browser-use-cloud}``.

The adapter intentionally pre-debits the Darwin's wallet for the
configured ``max_cost_usdc`` *before* calling the cloud — and passes the
same value to the SDK as ``max_cost_usd`` so the cloud's billing cap
matches the wallet's debit cap. Over-budget Darwins never reach the API.
"""

from __future__ import annotations

from typing import Any, Callable


# CAPTCHA / hostile-page detectors — same list as BrowserUseClient so a
# Darwin sees the same escalation surface across both adapters.
_DEFAULT_CAPTCHA_DETECTORS: tuple[str, ...] = (
    "hcaptcha",
    "recaptcha",
    "captcha",
    "cloudflare turnstile",
    "verify you are human",
)


class BrowserUseCloudClient:
    """Real :class:`BrowserClient` via Browser Use Cloud's V3 Agent.

    Args:
        wallet: :class:`darwin_dna.evolution.WalletPort` for billing.
            ``debit(amount, memo)`` is called *before* each session so
            an over-budget Darwin never reaches the cloud.
        api_key: Browser Use Cloud API key (``BROWSER_USE_API_KEY``).
            Ignored when ``client_factory`` is provided (tests).
        max_cost_usdc: Per-session spend cap. Passed to the SDK as
            ``max_cost_usd`` *and* debited from the wallet up-front so
            the ledger debit matches the cloud's billing cap.
        model: optional model name forwarded to the cloud (e.g. the BU
            custom model). ``None`` = let the cloud pick its default.
        client_factory: optional zero-arg callable returning a SDK-like
            object with a ``run(task, **kwargs)`` method. Used by tests
            to inject a stub; production calls the real SDK.
        captcha_detectors: substrings checked against an error's text;
            any match flips the outcome to ``blocked=captcha`` so the
            owner-escalation path triggers (same shape as
            :class:`BrowserUseClient`).
        timeout_seconds: per-task wall clock — caps both the HTTP call
            (SDK ``timeout``) and the agent's own ``max_steps`` budget
            indirectly via the cloud's defaults.
    """

    def __init__(
        self,
        *,
        wallet,
        api_key: str | None = None,
        max_cost_usdc: float = 0.10,
        model: str | None = None,
        client_factory: Callable[[], Any] | None = None,
        captcha_detectors: tuple[str, ...] = _DEFAULT_CAPTCHA_DETECTORS,
        timeout_seconds: float = 300.0,
    ) -> None:
        if max_cost_usdc <= 0:
            raise ValueError("max_cost_usdc must be positive")
        self._wallet = wallet
        self._api_key = api_key
        self._max_cost = float(max_cost_usdc)
        self._model = model
        self._client_factory = client_factory
        self._captcha = tuple(s.lower() for s in captcha_detectors)
        self._timeout = float(timeout_seconds)

    # -- BrowserClient surface --------------------------------------------
    def run(self, steps: list[str]) -> dict:
        # Debit *before* the network call so an over-budget Darwin
        # cannot rack up cloud costs we'd then have to chase down.
        self._wallet.debit(self._max_cost, "browser-use-cloud:session")
        task = self._task_from_steps(steps)
        try:
            client = self._build_client()
        except ImportError as exc:  # pragma: no cover - operator hint
            return {
                "ok": False,
                "detail": str(exc),
                "cost_usdc": self._max_cost,
            }

        try:
            kwargs: dict[str, Any] = {"max_cost_usd": self._max_cost}
            if self._model is not None:
                kwargs["model"] = self._model
            result = client.run(task, **kwargs)
        except Exception as exc:  # noqa: BLE001 — map any error to a Darwin outcome
            return self._error_outcome(exc)

        output = getattr(result, "output", None)
        if output is None:
            output = str(result)
        return {
            "ok": True,
            "data": {"output": output},
            "cost_usdc": self._max_cost,
        }

    # -- internals --------------------------------------------------------
    def _task_from_steps(self, steps: list[str]) -> str:
        """V3 takes one natural-language task; collapse the step list
        into a numbered prompt so the agent sees them in order."""
        if not steps:
            return ""
        if len(steps) == 1:
            return steps[0]
        return "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))

    def _build_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        try:
            from browser_use_sdk.v3 import BrowserUse  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "Browser Use Cloud adapter requires the [browser-use-cloud] "
                "extra: pip install 'darwin-dna[browser-use-cloud]'"
            ) from exc
        return BrowserUse(api_key=self._api_key, timeout=self._timeout)

    def _error_outcome(self, exc: Exception) -> dict:
        detail = str(exc)
        low = detail.lower()
        if any(k in low for k in self._captcha):
            return {
                "blocked": "captcha",
                "detail": detail,
                "cost_usdc": self._max_cost,
            }
        return {"ok": False, "detail": detail, "cost_usdc": self._max_cost}
