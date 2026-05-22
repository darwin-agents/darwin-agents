"""Tests for the CLI builders that wire sinks + sanctions providers."""

from __future__ import annotations

import argparse

import pytest

from darwin_platform.__main__ import (
    _build_alert_sink,
    _build_notification_sink,
    _build_sanctions,
    build_parser,
)
from darwin_platform.payments.sanctions import (
    CachedScreener,
    LocalDenylistScreener,
)
from darwin_platform.observability.sinks import (
    FanOutAlertSink,
    FanOutNotificationSink,
    GenericWebhookAlertSink,
    GenericWebhookNotificationSink,
    PagerDutyAlertSink,
    SlackWebhookNotificationSink,
)


def _ns(**kw):
    defaults = dict(
        sanctions_provider="local", sanctions_api_key=None,
        sanctions_denylist=None,
        slack_webhook_url=None, escalation_webhook_url=None,
        alert_webhook_url=None, pagerduty_routing_key=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# _build_sanctions
# ---------------------------------------------------------------------------

def test_build_sanctions_default_none():
    assert _build_sanctions(_ns()) is None


def test_build_sanctions_local_from_file(tmp_path):
    path = tmp_path / "deny.json"
    path.write_text('{"denylist": ["0xbad"], "denied_prefixes": []}')
    s = _build_sanctions(_ns(sanctions_denylist=str(path)))
    assert isinstance(s, LocalDenylistScreener)
    assert s.check("0xbad").allowed is False


def test_build_sanctions_chainalysis_wraps_in_cache():
    s = _build_sanctions(_ns(
        sanctions_provider="chainalysis", sanctions_api_key="k",
    ))
    assert isinstance(s, CachedScreener)


def test_build_sanctions_trm_wraps_in_cache():
    s = _build_sanctions(_ns(
        sanctions_provider="trm", sanctions_api_key="k",
    ))
    assert isinstance(s, CachedScreener)


def test_build_sanctions_chainalysis_requires_api_key():
    """Without --sanctions-api-key the CLI exits with code 2."""
    with pytest.raises(SystemExit) as excinfo:
        _build_sanctions(_ns(sanctions_provider="chainalysis"))
    assert excinfo.value.code == 2


def test_build_sanctions_picks_up_env_var(monkeypatch):
    monkeypatch.setenv("DARWIN_SANCTIONS_API_KEY", "env-key")
    s = _build_sanctions(_ns(sanctions_provider="chainalysis"))
    assert isinstance(s, CachedScreener)


# ---------------------------------------------------------------------------
# _build_notification_sink
# ---------------------------------------------------------------------------

def test_notification_sink_none_when_no_flags():
    assert _build_notification_sink(_ns()) is None


def test_notification_sink_slack_only():
    s = _build_notification_sink(
        _ns(slack_webhook_url="https://hooks.slack.com/x"),
    )
    assert isinstance(s, SlackWebhookNotificationSink)


def test_notification_sink_webhook_only():
    s = _build_notification_sink(
        _ns(escalation_webhook_url="https://hook.example/x"),
    )
    assert isinstance(s, GenericWebhookNotificationSink)


def test_notification_sink_fans_out_when_both_set():
    s = _build_notification_sink(
        _ns(
            slack_webhook_url="https://hooks.slack.com/x",
            escalation_webhook_url="https://hook.example/x",
        ),
    )
    assert isinstance(s, FanOutNotificationSink)


# ---------------------------------------------------------------------------
# _build_alert_sink
# ---------------------------------------------------------------------------

def test_alert_sink_none_when_no_flags():
    assert _build_alert_sink(_ns()) is None


def test_alert_sink_webhook_only():
    s = _build_alert_sink(_ns(alert_webhook_url="https://alerts/x"))
    assert isinstance(s, GenericWebhookAlertSink)


def test_alert_sink_pagerduty_only():
    s = _build_alert_sink(_ns(pagerduty_routing_key="rk"))
    assert isinstance(s, PagerDutyAlertSink)


def test_alert_sink_fans_out_when_both_set():
    s = _build_alert_sink(_ns(
        alert_webhook_url="https://alerts/x",
        pagerduty_routing_key="rk",
    ))
    assert isinstance(s, FanOutAlertSink)


# ---------------------------------------------------------------------------
# CLI parser surface
# ---------------------------------------------------------------------------

def test_cli_accepts_all_new_flags():
    parser = build_parser()
    args = parser.parse_args([
        "serve",
        "--sanctions-provider", "chainalysis",
        "--sanctions-api-key", "k",
        "--slack-webhook-url", "https://hooks.slack.com/x",
        "--escalation-webhook-url", "https://w/e",
        "--alert-webhook-url", "https://w/a",
        "--pagerduty-routing-key", "rk",
    ])
    assert args.sanctions_provider == "chainalysis"
    assert args.sanctions_api_key == "k"
    assert args.slack_webhook_url == "https://hooks.slack.com/x"
    assert args.escalation_webhook_url == "https://w/e"
    assert args.alert_webhook_url == "https://w/a"
    assert args.pagerduty_routing_key == "rk"


def test_cli_sanctions_provider_choices_enforced():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["serve", "--sanctions-provider", "bogus"])
