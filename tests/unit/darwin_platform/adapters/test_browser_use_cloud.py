"""Browser Use Cloud V3 Agent adapter (docs/engineering/15, 31).

Exercises the adapter against a stub SDK client (no network, no SDK
import) so the test runs on any install. The real SDK shape is covered
by a single ImportError-path probe; everything else uses the injectable
``client_factory`` seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from darwin_platform.adapters.browser import BrowserClient
from darwin_platform.adapters.browser_use_cloud import BrowserUseCloudClient


@dataclass
class _StubWallet:
    debits: list[tuple[float, str]]

    def debit(self, amount: float, memo: str) -> None:
        self.debits.append((float(amount), memo))


class _StubResult:
    """Mimics what the real SDK returns from ``BrowserUse.run(...)``."""

    def __init__(self, output: Any):
        self.output = output


class _StubClient:
    """SDK stand-in. Records call args so tests can assert on them."""

    def __init__(self, *, result: Any = None, raises: Exception | None = None):
        self._result = result
        self._raises = raises
        self.calls: list[tuple[str, dict]] = []

    def run(self, task: str, **kwargs: Any) -> Any:
        self.calls.append((task, dict(kwargs)))
        if self._raises is not None:
            raise self._raises
        return self._result


def _adapter(*, wallet=None, client=None, **kw) -> BrowserUseCloudClient:
    wallet = wallet or _StubWallet([])
    client = client or _StubClient(result=_StubResult("ok"))
    return BrowserUseCloudClient(
        wallet=wallet,
        api_key="bu_test",
        client_factory=lambda: client,
        **kw,
    )


def test_adapter_satisfies_browser_client_protocol():
    """Same Protocol as BrowserUseClient → the genome neuron can't tell
    them apart, which is the whole point of provider-neutrality."""
    assert isinstance(_adapter(), BrowserClient)


def test_run_success_returns_ok_outcome_and_debits_wallet():
    wallet = _StubWallet([])
    client = _StubClient(result=_StubResult("page title: Example"))
    a = _adapter(wallet=wallet, client=client, max_cost_usdc=0.05)
    out = a.run(["visit example.com", "extract the page title"])
    assert out == {
        "ok": True,
        "data": {"output": "page title: Example"},
        "cost_usdc": 0.05,
    }
    # Wallet debited once, before the API call (debit happens up-front).
    assert wallet.debits == [(0.05, "browser-use-cloud:session")]


def test_steps_are_collapsed_into_a_numbered_task():
    """V3 takes one natural-language task; the adapter numbers the
    multi-step list so order is preserved when the cloud agent reads it."""
    client = _StubClient(result=_StubResult("done"))
    _adapter(client=client).run(["go to A", "click B", "extract C"])
    [(task, _kw)] = client.calls
    assert task == "1. go to A\n2. click B\n3. extract C"


def test_single_step_is_passed_verbatim():
    client = _StubClient(result=_StubResult("ok"))
    _adapter(client=client).run(["List the top 5 posts on HN"])
    [(task, _)] = client.calls
    assert task == "List the top 5 posts on HN"  # no numbering for 1-step


def test_max_cost_and_model_are_forwarded_to_sdk():
    client = _StubClient(result=_StubResult("ok"))
    a = _adapter(client=client, max_cost_usdc=0.25, model="bu-small")
    a.run(["do thing"])
    [(_task, kw)] = client.calls
    assert kw["max_cost_usd"] == 0.25
    assert kw["model"] == "bu-small"


def test_no_model_means_no_model_kwarg_to_sdk():
    """We omit ``model`` entirely when unset so the SDK picks its
    default — passing ``None`` would error on stricter SDK versions."""
    client = _StubClient(result=_StubResult("ok"))
    _adapter(client=client).run(["x"])
    [(_task, kw)] = client.calls
    assert "model" not in kw
    assert "max_cost_usd" in kw


def test_captcha_keyword_in_error_maps_to_blocked_outcome():
    """A failure whose detail mentions a CAPTCHA keyword flips to
    ``blocked=captcha`` so the genome's escalation path runs — same
    contract as the in-process BrowserUseClient."""
    client = _StubClient(raises=RuntimeError("hit a reCAPTCHA challenge"))
    out = _adapter(client=client, max_cost_usdc=0.02).run(["x"])
    assert out["blocked"] == "captcha"
    assert "recaptcha" in out["detail"].lower()
    assert out["cost_usdc"] == 0.02


def test_generic_error_maps_to_navigation_failed_outcome():
    client = _StubClient(raises=RuntimeError("page structure changed"))
    out = _adapter(client=client).run(["x"])
    assert out["ok"] is False
    assert "structure" in out["detail"]


def test_wallet_is_debited_even_when_cloud_call_fails():
    """Pre-debit policy: an over-budget Darwin must not reach the cloud,
    so the debit happens before the call. A failure mid-call still
    leaves the debit in place (the cloud may have done partial work)."""
    wallet = _StubWallet([])
    client = _StubClient(raises=RuntimeError("nope"))
    _adapter(wallet=wallet, client=client, max_cost_usdc=0.03).run(["x"])
    assert wallet.debits == [(0.03, "browser-use-cloud:session")]


def test_non_positive_max_cost_rejected():
    with pytest.raises(ValueError, match="max_cost_usdc must be positive"):
        BrowserUseCloudClient(wallet=_StubWallet([]), max_cost_usdc=0)


def test_result_without_output_attribute_falls_back_to_str():
    """The SDK's result shape may evolve; treat anything that doesn't
    expose ``.output`` as a string so the Protocol contract holds."""
    client = _StubClient(result="bare-string-result")
    out = _adapter(client=client).run(["x"])
    assert out["ok"] is True
    assert out["data"]["output"] == "bare-string-result"
