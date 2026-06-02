"""Tests for the real browser_use wiring (docs/engineering/15).

The genome calls a tiny synchronous ``run(steps) -> dict`` surface, but the real
browser_use SDK is async, takes the task at construction, connects via a
BrowserSession, and returns an AgentHistoryList. BrowserbaseAgentRunner bridges
that gap. We drive it with fakes (no real SDK / browser / network) so the
prompt→task mapping, session wiring, vision flag, result normalization, and the
captcha→live-view handoff are all pinned.
"""

from __future__ import annotations

import pytest

from darwin_platform.adapters.browser import (
    BrowserUseClient,
    BrowserbaseAgentRunner,
    browserbase_connect_url,
)


# ---------------------------------------------------------------------------
# connect URL
# ---------------------------------------------------------------------------

def test_connect_url_from_api_key():
    url = browserbase_connect_url(api_key="bb_secret")
    assert url.startswith("wss://connect.browserbase.com?")
    assert "apiKey=bb_secret" in url


def test_connect_url_explicit_url_wins():
    assert browserbase_connect_url(
        api_key="k", url="wss://self-hosted/cdp"
    ) == "wss://self-hosted/cdp"


def test_connect_url_requires_key_or_url():
    with pytest.raises(ValueError):
        browserbase_connect_url()


# ---------------------------------------------------------------------------
# runner — fakes for the real SDK
# ---------------------------------------------------------------------------

class _FakeHistory:
    def __init__(self, *, successful=True, errors=None, result="done", steps=3):
        self._successful = successful
        self._errors = errors or []
        self._result = result
        self._steps = steps

    def is_successful(self):
        return self._successful

    def is_done(self):
        return True

    def has_errors(self):
        return bool(self._errors)

    def errors(self):
        return self._errors

    def final_result(self):
        return self._result

    def number_of_steps(self):
        return self._steps


class _FakeSession:
    def __init__(self, *, cdp_url):
        self.cdp_url = cdp_url


def _fake_agent_cls(history, captured):
    class _FakeAgent:
        def __init__(self, *, task, llm, browser_session, use_vision):
            captured.update(
                task=task, llm=llm, browser_session=browser_session,
                use_vision=use_vision,
            )

        def run(self, *, max_steps):
            captured["max_steps"] = max_steps
            return history  # sync → identity awaiter returns it

    return _FakeAgent


def _runner(history, captured, *, live_view_url=None, use_vision=False):
    return BrowserbaseAgentRunner(
        connect_url="wss://connect.browserbase.com?apiKey=k",
        llm=object(),
        agent_cls=_fake_agent_cls(history, captured),
        browser_session_cls=_FakeSession,
        use_vision=use_vision,
        live_view_url=live_view_url,
        awaiter=lambda r: r,  # fake run() is sync
    )


def test_runner_maps_prompt_to_task_and_normalizes_success():
    captured: dict = {}
    out = _runner(_FakeHistory(result="bought widget"), captured).run(
        "buy a widget", max_steps=7,
    )
    assert captured["task"] == "buy a widget"
    assert captured["max_steps"] == 7
    assert captured["browser_session"].cdp_url.startswith("wss://connect.browserbase.com")
    assert out["ok"] is True
    assert out["data"]["result"] == "bought widget"


def test_runner_propagates_vision_flag():
    captured: dict = {}
    _runner(_FakeHistory(), captured, use_vision=True).run("x")
    assert captured["use_vision"] is True


def test_runner_normalizes_failure_with_errors():
    out = _runner(
        _FakeHistory(successful=False, errors=["nav failed", None]), {},
    ).run("x")
    assert out["ok"] is False
    assert "nav failed" in out["detail"]


def test_runner_threads_live_view_url():
    out = _runner(
        _FakeHistory(successful=False, errors=["hit recaptcha"]),
        {}, live_view_url="https://browserbase.live/abc",
    ).run("x")
    assert out["data"]["live_view_url"] == "https://browserbase.live/abc"


# ---------------------------------------------------------------------------
# adapter — captcha block surfaces the live-view URL for human takeover
# ---------------------------------------------------------------------------

class _Wallet:
    def debit(self, amount, memo):
        return None


def test_adapter_surfaces_live_view_on_captcha():
    captured: dict = {}
    runner = _runner(
        _FakeHistory(successful=False, errors=["recaptcha challenge shown"]),
        captured, live_view_url="https://browserbase.live/xyz",
    )
    client = BrowserUseClient(wallet=_Wallet(), browser_factory=lambda: runner)
    out = client.run(["open site", "log in"])
    assert out["blocked"] == "captcha"
    assert out["live_view_url"] == "https://browserbase.live/xyz"
