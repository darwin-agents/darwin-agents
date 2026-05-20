"""Tests for the production notification + alert sinks (doc 08)."""

from __future__ import annotations

import types

import pytest

from darwin_platform.escalation import RoutedTicket, TicketStatus
from darwin_dna.constitution import TicketClass
from darwin_platform.monitoring import Alert, Severity
from darwin_platform.sinks import (
    FanOutAlertSink,
    FanOutNotificationSink,
    GenericWebhookAlertSink,
    GenericWebhookNotificationSink,
    PagerDutyAlertSink,
    SlackWebhookNotificationSink,
    SMTPEmailNotificationSink,
)


def _ticket():
    return RoutedTicket(
        ticket_id="t-1",
        darwin_id="d-1",
        neuron="browser.operator",
        ticket_class=TicketClass.CAPTCHA,
        opened_ts=1000.0,
        deadline_ts=1900.0,
        detail={"reason": "hCaptcha on signup"},
        status=TicketStatus.OPEN,
        tick=1,
    )


def _alert(*, kind="spend_velocity", severity=Severity.WARN):
    return Alert(
        darwin_id="d-1", kind=kind, severity=severity,
        suggested_level=2, detail={"x": 1}, ts=1000.0,
    )


class _StubHTTP:
    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        return types.SimpleNamespace(status_code=self.status, text="")


# ---------------------------------------------------------------------------
# GenericWebhookNotificationSink
# ---------------------------------------------------------------------------

def test_webhook_notification_posts_json():
    http = _StubHTTP()
    s = GenericWebhookNotificationSink(url="https://hook.example/x",
                                       http_client=http)
    s.notify(_ticket())
    assert len(http.calls) == 1
    assert http.calls[0]["method"] == "POST"
    assert http.calls[0]["url"] == "https://hook.example/x"
    assert s.delivered == 1


def test_webhook_notification_absorbs_http_error():
    """Backend 5xx must NOT raise to the supervisor."""
    http = _StubHTTP(status=500)
    s = GenericWebhookNotificationSink(url="https://hook.example/x",
                                       http_client=http)
    s.notify(_ticket())  # no raise
    assert s.delivered == 0
    assert len(s.errors) == 1
    assert "500" in s.errors[0]["message"]


def test_webhook_notification_absorbs_network_error():
    class _BrokenHTTP:
        def request(self, *a, **k): raise ConnectionError("dns")

    s = GenericWebhookNotificationSink(url="https://hook.example/x",
                                       http_client=_BrokenHTTP())
    s.notify(_ticket())
    assert s.delivered == 0
    assert len(s.errors) == 1


def test_webhook_notification_empty_url_rejected():
    with pytest.raises(ValueError):
        GenericWebhookNotificationSink(url="", http_client=_StubHTTP())


# ---------------------------------------------------------------------------
# SlackWebhookNotificationSink
# ---------------------------------------------------------------------------

def test_slack_payload_shape():
    http = _StubHTTP()
    s = SlackWebhookNotificationSink(
        webhook_url="https://hooks.slack.com/services/x/y/z",
        http_client=http,
    )
    s.notify(_ticket())
    body = http.calls[0]["kwargs"]["content"]
    assert "Darwin `d-1`" in body
    assert "browser.operator" in body
    assert s.delivered == 1


# ---------------------------------------------------------------------------
# SMTPEmailNotificationSink
# ---------------------------------------------------------------------------

def test_smtp_email_sent():
    class _SMTP:
        def __init__(self):
            self.sent = []
        def send_message(self, msg):
            self.sent.append(msg)
        def quit(self):
            pass

    smtp = _SMTP()
    s = SMTPEmailNotificationSink(
        to_address="alex@example.com",
        from_address="darwin@example.com",
        smtp_factory=lambda: smtp,
    )
    s.notify(_ticket())
    assert len(smtp.sent) == 1
    msg = smtp.sent[0]
    assert msg["To"] == "alex@example.com"
    assert msg["From"] == "darwin@example.com"
    assert "d-1" in msg["Subject"]
    assert s.delivered == 1


def test_smtp_error_absorbed():
    class _Broken:
        def send_message(self, msg): raise OSError("smtp down")
        def quit(self): pass

    s = SMTPEmailNotificationSink(
        to_address="x@x", from_address="y@y",
        smtp_factory=lambda: _Broken(),
    )
    s.notify(_ticket())
    assert s.delivered == 0
    assert len(s.errors) == 1


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------

def test_fanout_calls_each_sink():
    http_a = _StubHTTP()
    http_b = _StubHTTP()
    a = GenericWebhookNotificationSink(url="https://a/", http_client=http_a)
    b = GenericWebhookNotificationSink(url="https://b/", http_client=http_b)
    fan = FanOutNotificationSink([a, b])
    fan.notify(_ticket())
    assert len(http_a.calls) == 1
    assert len(http_b.calls) == 1


def test_fanout_isolation_one_failure_doesnt_block_others():
    class _Broken:
        def notify(self, t): raise RuntimeError("kaboom")

    http = _StubHTTP()
    good = GenericWebhookNotificationSink(url="https://ok/", http_client=http)
    fan = FanOutNotificationSink([_Broken(), good])
    fan.notify(_ticket())
    # good sink delivered despite the broken one raising
    assert good.delivered == 1
    assert len(fan.errors) == 1


# ---------------------------------------------------------------------------
# Alert sinks
# ---------------------------------------------------------------------------

def test_alert_webhook_posts_json():
    http = _StubHTTP()
    s = GenericWebhookAlertSink(url="https://alerts/", http_client=http)
    s.emit(_alert())
    assert s.delivered == 1
    assert http.calls[0]["url"] == "https://alerts/"


def test_pagerduty_only_fires_at_min_severity():
    http = _StubHTTP()
    s = PagerDutyAlertSink(
        routing_key="rk-1", http_client=http, min_severity="critical",
    )
    s.emit(_alert(severity=Severity.WARN))
    assert len(http.calls) == 0  # below threshold
    s.emit(_alert(severity=Severity.CRITICAL))
    assert len(http.calls) == 1
    body = http.calls[0]["kwargs"]["content"]
    assert "rk-1" in body
    assert "trigger" in body


def test_pagerduty_uses_dedup_key():
    http = _StubHTTP()
    s = PagerDutyAlertSink(routing_key="rk", http_client=http)
    s.emit(_alert(kind="spend_velocity", severity=Severity.CRITICAL))
    import json
    body = json.loads(http.calls[0]["kwargs"]["content"])
    assert body["dedup_key"] == "d-1:spend_velocity"


def test_alert_fanout():
    http_a = _StubHTTP()
    http_b = _StubHTTP()
    a = GenericWebhookAlertSink(url="https://a/", http_client=http_a)
    b = GenericWebhookAlertSink(url="https://b/", http_client=http_b)
    fan = FanOutAlertSink([a, b])
    fan.emit(_alert(severity=Severity.CRITICAL))
    assert len(http_a.calls) == 1
    assert len(http_b.calls) == 1
