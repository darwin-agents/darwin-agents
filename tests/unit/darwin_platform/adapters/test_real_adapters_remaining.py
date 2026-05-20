"""Tests for the remaining real-provider adapters (docs/engineering/14, 15, 17).

All three use stub clients — no network, no SDK install required. The
adapters' job here is translation: turn the genome's contract into the
underlying SDK's calls.
"""

from __future__ import annotations

import pytest

from darwin_fakes.fake_wallet import FakeWallet


def _wallet(opening: float = 100.0):
    return FakeWallet(opening_usdc=opening, clock=lambda: 0.0)


# ============================================================================
# BrowserUseClient
# ============================================================================
class _StubAgent:
    """Mimics browser_use.Agent enough for the adapter's translation."""

    def __init__(self, *, ok=True, error="", data=None, raise_on_run=None):
        self._ok = ok
        self._error = error
        self._data = data or {}
        self._raise = raise_on_run
        self.last_prompt: str | None = None

    def run(self, prompt: str):
        self.last_prompt = prompt
        if self._raise:
            raise self._raise

        class Result:
            success = self._ok
            error_message = self._error
            data = self._data

        return Result()


def test_browser_success_translates_to_genome_shape():
    from darwin_platform.adapters import BrowserUseClient

    agent = _StubAgent(ok=True, data={"published": True, "url": "https://x"})
    client = BrowserUseClient(
        wallet=_wallet(), browser_factory=lambda: agent,
    )
    out = client.run(["open https://x", "click publish"])
    assert out["ok"] is True
    assert out["data"]["url"] == "https://x"
    assert out["cost_usdc"] == pytest.approx(0.01)
    # Steps were joined into a single prompt for the Agent.
    assert agent.last_prompt and "publish" in agent.last_prompt


def test_browser_captcha_translates_to_blocked():
    """Doc-15-flavored: human-verification walls become an escalate
    signal for the genome's ``browser.operator`` neuron — NOT a hard
    failure."""
    from darwin_platform.adapters import BrowserUseClient

    agent = _StubAgent(ok=False, error="page shows hCaptcha challenge")
    client = BrowserUseClient(
        wallet=_wallet(), browser_factory=lambda: agent,
    )
    out = client.run(["open https://x"])
    assert "blocked" in out and out["blocked"] == "captcha"
    assert out["cost_usdc"] == pytest.approx(0.01)


def test_browser_navigation_failure_translates_to_ok_false():
    from darwin_platform.adapters import BrowserUseClient

    agent = _StubAgent(ok=False, error="page not reachable")
    client = BrowserUseClient(
        wallet=_wallet(), browser_factory=lambda: agent,
    )
    out = client.run(["open https://x"])
    assert out["ok"] is False
    assert "reachable" in out["detail"]


def test_browser_exception_in_run_classifies_captcha_or_failure():
    from darwin_platform.adapters import BrowserUseClient

    # Captcha-like exception.
    agent = _StubAgent(raise_on_run=RuntimeError("recaptcha frame detected"))
    out = BrowserUseClient(
        wallet=_wallet(), browser_factory=lambda: agent,
    ).run(["x"])
    assert out.get("blocked") == "captcha"

    # Plain failure.
    agent = _StubAgent(raise_on_run=RuntimeError("dns timeout"))
    out = BrowserUseClient(
        wallet=_wallet(), browser_factory=lambda: agent,
    ).run(["x"])
    assert out["ok"] is False
    assert "dns" in out["detail"]


def test_browser_billing_refused_before_session():
    """Out-of-funds Darwins should never touch Browserbase — match the
    FakeBrowser's exact behaviour."""
    from darwin_platform.adapters import BrowserUseClient

    poor = _wallet(opening=0.0)
    factory_called = {"n": 0}

    def factory():
        factory_called["n"] += 1
        return _StubAgent()

    client = BrowserUseClient(
        wallet=poor, browser_factory=factory,
    )
    out = client.run(["x"])
    assert out["ok"] is False
    assert "no funds" in out["detail"].lower()
    assert factory_called["n"] == 0  # never reached the factory


def test_browser_dict_result_shape_supported():
    """Some browser-use versions return a plain dict; the adapter
    accepts that shape too."""
    from darwin_platform.adapters import BrowserUseClient

    class DictAgent:
        def run(self, prompt):
            return {"ok": True, "data": {"x": 1}}

    out = BrowserUseClient(
        wallet=_wallet(), browser_factory=DictAgent,
    ).run(["x"])
    assert out["ok"] is True
    assert out["data"] == {"x": 1}


# ============================================================================
# E2BSandbox
# ============================================================================
class _StubSandbox:
    def __init__(self, *, exit_code=0, stdout="", stderr="", raise_on_run=None):
        self.exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self._raise = raise_on_run
        self.closed = False
        self.last_code: str | None = None

    def run_code(self, *, code, language, timeout):
        self.last_code = code
        if self._raise:
            raise self._raise
        return {
            "exit_code": self.exit_code,
            "stdout": self._stdout,
            "stderr": self._stderr,
            "ok": self.exit_code == 0,
        }

    def close(self):
        self.closed = True


def test_sandbox_success_returns_stdout():
    from darwin_platform.adapters import E2BSandbox

    sandbox = _StubSandbox(stdout="hello\n")
    runner = E2BSandbox(
        wallet=_wallet(), sandbox_factory=lambda: sandbox,
    )
    out = runner.run_code(language="python", code="print('hello')")
    assert out["ok"] is True
    assert out["stdout"] == "hello\n"
    assert out["exit_code"] == 0
    assert sandbox.closed is True  # cleanup ran
    assert sandbox.last_code == "print('hello')"


def test_sandbox_nonzero_exit_is_failure():
    from darwin_platform.adapters import E2BSandbox

    sandbox = _StubSandbox(exit_code=1, stderr="Traceback...")
    out = E2BSandbox(
        wallet=_wallet(), sandbox_factory=lambda: sandbox,
    ).run_code(language="python", code="1/0")
    assert out["ok"] is False
    assert out["exit_code"] == 1
    assert "Traceback" in out["stderr"]


def test_sandbox_exception_cleans_up_and_reports():
    from darwin_platform.adapters import E2BSandbox

    sandbox = _StubSandbox(raise_on_run=RuntimeError("vm crashed"))
    out = E2BSandbox(
        wallet=_wallet(), sandbox_factory=lambda: sandbox,
    ).run_code(language="python", code="x")
    assert out["ok"] is False
    assert "vm crashed" in out["stderr"]
    assert sandbox.closed is True  # cleanup ran even on error


def test_sandbox_billing_refused_before_init():
    from darwin_platform.adapters import E2BSandbox

    poor = _wallet(opening=0.0)
    factory_called = {"n": 0}

    def factory():
        factory_called["n"] += 1
        return _StubSandbox()

    out = E2BSandbox(
        wallet=poor, sandbox_factory=factory,
    ).run_code(language="python", code="x")
    assert out["ok"] is False
    assert factory_called["n"] == 0


def test_sandbox_init_failure_does_not_double_close():
    """If sandbox_factory() itself raises, we shouldn't try to close a
    sandbox that was never opened — silent return is the right shape."""
    from darwin_platform.adapters import E2BSandbox

    def factory():
        raise RuntimeError("e2b not provisioned")

    out = E2BSandbox(
        wallet=_wallet(), sandbox_factory=factory,
    ).run_code(language="python", code="x")
    assert out["ok"] is False
    assert "not provisioned" in out["stderr"]


# ============================================================================
# X402PaymentClient
# ============================================================================
class _StubResponse:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class _StubHTTP:
    """Scripts a sequence of responses; tests assert each call's args."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0)


def test_x402_passthrough_when_status_is_not_402():
    from darwin_platform.adapters import X402PaymentClient

    http = _StubHTTP([_StubResponse(200, headers={})])
    client = X402PaymentClient(http=http, wallet=_wallet())
    resp = client.request("GET", "https://api/x")
    assert resp.status_code == 200
    assert len(http.calls) == 1  # no retry, no payment


def test_x402_pays_then_retries():
    from darwin_platform.adapters import X402PaymentClient

    payment_required = _StubResponse(
        402,
        headers={
            "X-Payment-Required-Amount-Usdc": "0.50",
            "X-Payment-Required-Address": "0x" + "ab" * 20,
            "X-Payment-Required-Nonce": "nonce-123",
        },
    )
    success = _StubResponse(200, headers={})
    http = _StubHTTP([payment_required, success])
    w = _wallet(opening=10.0)
    client = X402PaymentClient(http=http, wallet=w)
    resp = client.request("GET", "https://api/paywall")
    assert resp.status_code == 200
    assert len(http.calls) == 2  # original + paid retry
    # The retry carried the tx + nonce headers.
    retry = http.calls[1]
    assert "X-Payment-Tx" in retry["headers"]
    assert retry["headers"]["X-Payment-Required-Nonce"] == "nonce-123"
    # The wallet was debited $0.50.
    assert w.spent_lifetime_usdc() == pytest.approx(0.50)


def test_x402_over_cap_refuses():
    from darwin_platform.adapters import X402Error, X402PaymentClient

    pr = _StubResponse(
        402,
        headers={
            "X-Payment-Required-Amount-Usdc": "50.00",
            "X-Payment-Required-Address": "0x" + "ab" * 20,
        },
    )
    http = _StubHTTP([pr])
    client = X402PaymentClient(
        http=http, wallet=_wallet(opening=100.0),
        max_payment_usdc=5.0,
    )
    with pytest.raises(X402Error, match="> client cap"):
        client.request("GET", "https://api/expensive")


def test_x402_missing_headers_refused():
    from darwin_platform.adapters import X402Error, X402PaymentClient

    pr = _StubResponse(
        402, headers={"X-Payment-Required-Amount-Usdc": "1.0"},
    )  # missing address
    http = _StubHTTP([pr])
    with pytest.raises(X402Error, match="missing required headers"):
        X402PaymentClient(
            http=http, wallet=_wallet()
        ).request("GET", "https://api/x")


def test_x402_invalid_amount_refused():
    from darwin_platform.adapters import X402Error, X402PaymentClient

    pr = _StubResponse(
        402,
        headers={
            "X-Payment-Required-Amount-Usdc": "not-a-number",
            "X-Payment-Required-Address": "0x" + "ab" * 20,
        },
    )
    http = _StubHTTP([pr])
    with pytest.raises(X402Error, match="not a number"):
        X402PaymentClient(
            http=http, wallet=_wallet()
        ).request("GET", "https://api/x")


def test_x402_retry_budget_exhaustion():
    """A misconfigured upstream that returns 402 even after payment
    eventually exhausts the retry budget — we refuse rather than loop
    the wallet dry."""
    from darwin_platform.adapters import X402Error, X402PaymentClient

    hdrs = {
        "X-Payment-Required-Amount-Usdc": "0.10",
        "X-Payment-Required-Address": "0x" + "ab" * 20,
    }
    # Two 402s in a row with max_retries=1: pay once, retry, still 402,
    # raise on the next iteration.
    http = _StubHTTP([
        _StubResponse(402, headers=hdrs),
        _StubResponse(402, headers=hdrs),
    ])
    client = X402PaymentClient(
        http=http, wallet=_wallet(opening=10.0),
        max_retries=1,
    )
    with pytest.raises(X402Error, match="retry budget exhausted"):
        client.request("GET", "https://api/broken")


def test_x402_wallet_refusal_surfaces_as_X402Error():
    from darwin_platform.adapters import X402Error, X402PaymentClient

    pr = _StubResponse(
        402,
        headers={
            "X-Payment-Required-Amount-Usdc": "1.0",
            "X-Payment-Required-Address": "0x" + "ab" * 20,
        },
    )
    http = _StubHTTP([pr])
    poor = _wallet(opening=0.0)
    with pytest.raises(X402Error, match="wallet refused"):
        X402PaymentClient(http=http, wallet=poor).request("GET", "https://x")


def test_x402_custom_header_names():
    """Different x402 deployments use different prefix conventions; the
    adapter accepts overrides."""
    from darwin_platform.adapters import X402PaymentClient

    pr = _StubResponse(
        402,
        headers={
            "Pay-Amount": "0.25",
            "Pay-To": "0x" + "ab" * 20,
        },
    )
    http = _StubHTTP([pr, _StubResponse(200)])
    client = X402PaymentClient(
        http=http, wallet=_wallet(opening=10.0),
        header_names={
            "amount": "Pay-Amount",
            "address": "Pay-To",
            "nonce": "Pay-Nonce",
            "tx": "Pay-Tx",
        },
    )
    resp = client.request("GET", "https://api/x")
    assert resp.status_code == 200
    assert "Pay-Tx" in http.calls[1]["headers"]
