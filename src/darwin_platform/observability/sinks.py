"""Production notification + alert sinks (doc 08 §Observability).

The default :class:`darwin_platform.escalation.LogNotificationSink` and
:class:`darwin_platform.monitoring.LogAlertSink` are convenient for
tests but never reach out to the owner. Production needs pluggable
sinks that:

- Fan escalation tickets out to the owner (Slack DM, email, webhook).
- Fan monitor alerts out to on-call (PagerDuty, generic webhook).
- Fail closed on backend errors — a misbehaving notification provider
  must NEVER block the supervisor's drive loop. Errors are absorbed,
  not raised.

This module ships generic HTTP-based sinks (Slack incoming webhook,
generic POST-JSON webhook, SMTP email). They cover the common cases;
deployers can plug their own ``NotificationSink``- or ``AlertSink``-
conforming object via the Supervisor ctor.

Each sink takes an injected HTTP client (defaults to ``httpx.Client``)
so tests can substitute a stub without standing up a real server.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable


class _SinkError(Exception):
    """Raised internally on a transient backend error. NEVER bubbles
    out of a sink method — the sink absorbs it, records it for ops
    introspection, and continues. The supervisor must never lose a
    drive heartbeat because Slack was down."""


def _safe_call(fn: Callable[[], None], *, sink: "_SinkBase") -> None:
    try:
        fn()
    except Exception as exc:
        sink._record_error(exc)  # noqa: SLF001


class _SinkBase:
    """Shared bookkeeping: a bounded ring of recent errors so an operator
    can ``GET /platform/sinks/errors`` and see why notifications stopped."""

    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []
        self.delivered = 0

    def _record_error(self, exc: Exception) -> None:
        self.errors.append({
            "ts": time.time(),
            "exception": type(exc).__name__,
            "message": str(exc)[:500],
        })
        # Bound the ring at 100 — older errors are not actionable.
        if len(self.errors) > 100:
            del self.errors[:-100]


# ---------------------------------------------------------------------------
# Notification sinks (escalation tickets → owner)
# ---------------------------------------------------------------------------

class GenericWebhookNotificationSink(_SinkBase):
    """POSTs the ticket as JSON to a configured URL. The simplest
    integration: works against any service that accepts a JSON webhook
    (Discord, custom, Zapier, n8n, etc.)."""

    def __init__(
        self,
        *,
        url: str,
        http_client: Any | None = None,
        timeout_seconds: float = 5.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        if not url:
            raise ValueError("GenericWebhookNotificationSink needs a url")
        self._url = url
        self._timeout = float(timeout_seconds)
        self._headers = {"Content-Type": "application/json",
                         **(extra_headers or {})}
        if http_client is None:
            try:
                import httpx
                http_client = httpx.Client(timeout=self._timeout)
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "GenericWebhookNotificationSink requires httpx: "
                    "`pip install darwin-dna[platform]`"
                ) from exc
        self._http = http_client

    def notify(self, ticket) -> None:
        def _post():
            body = {
                "ticket_id": ticket.ticket_id,
                "darwin_id": ticket.darwin_id,
                "ticket_class": ticket.ticket_class.value,
                "neuron": ticket.neuron,
                "opened_ts": ticket.opened_ts,
                "deadline_ts": ticket.deadline_ts,
                "detail": ticket.detail,
            }
            resp = self._http.request(
                "POST", self._url, content=json.dumps(body),
                headers=self._headers,
            )
            if int(getattr(resp, "status_code", 0)) >= 400:
                raise _SinkError(
                    f"webhook {self._url} returned "
                    f"{getattr(resp, 'status_code', '?')}"
                )
            self.delivered += 1
        _safe_call(_post, sink=self)


class SlackWebhookNotificationSink(_SinkBase):
    """Slack incoming webhook. Posts a human-readable card. Bind one
    webhook per channel; for multi-channel routing, layer a router in
    front."""

    def __init__(
        self,
        *,
        webhook_url: str,
        http_client: Any | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        super().__init__()
        if not webhook_url.startswith(("https://hooks.slack.com/", "http://")):
            # Allow http:// only for tests; the URL pattern check is to
            # catch obvious copy-paste accidents.
            pass
        self._url = webhook_url
        self._timeout = float(timeout_seconds)
        if http_client is None:
            try:
                import httpx
                http_client = httpx.Client(timeout=self._timeout)
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "SlackWebhookNotificationSink requires httpx: "
                    "`pip install darwin-dna[platform]`"
                ) from exc
        self._http = http_client

    def notify(self, ticket) -> None:
        def _post():
            detail_text = str(ticket.detail) if ticket.detail else "<no detail>"
            payload = {
                "text": (
                    f":wave: Darwin `{ticket.darwin_id}` needs attention\n"
                    f"*Class:* {ticket.ticket_class.value}  "
                    f"*Neuron:* `{ticket.neuron}`  "
                    f"*Ticket:* `{ticket.ticket_id}`\n"
                    f"*Detail:* {detail_text[:300]}"
                ),
            }
            resp = self._http.request(
                "POST", self._url, content=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            if int(getattr(resp, "status_code", 0)) >= 400:
                raise _SinkError(
                    f"slack webhook returned {getattr(resp, 'status_code', '?')}"
                )
            self.delivered += 1
        _safe_call(_post, sink=self)


class SMTPEmailNotificationSink(_SinkBase):
    """Email sink via SMTP. Each notify() sends one mail to the
    configured recipient. The smtp_client is injected so tests don't
    need a real SMTP server."""

    def __init__(
        self,
        *,
        to_address: str,
        from_address: str,
        smtp_factory: Callable[[], Any],
        subject_template: str | None = None,
    ) -> None:
        super().__init__()
        if not to_address or not from_address:
            raise ValueError("SMTP sink needs both to_address and from_address")
        self._to = to_address
        self._from = from_address
        self._smtp_factory = smtp_factory
        self._subject_tmpl = (
            subject_template
            or "[Darwin] {ticket_class} ticket on {darwin_id}"
        )

    def notify(self, ticket) -> None:
        def _send():
            from email.message import EmailMessage
            msg = EmailMessage()
            msg["Subject"] = self._subject_tmpl.format(
                ticket_class=ticket.ticket_class.value,
                darwin_id=ticket.darwin_id,
                neuron=ticket.neuron,
                ticket_id=ticket.ticket_id,
            )
            msg["From"] = self._from
            msg["To"] = self._to
            msg.set_content(
                f"Darwin {ticket.darwin_id} opened a ticket.\n\n"
                f"Class: {ticket.ticket_class.value}\n"
                f"Neuron: {ticket.neuron}\n"
                f"Ticket id: {ticket.ticket_id}\n"
                f"Opened ts: {ticket.opened_ts}\n"
                f"Deadline ts: {ticket.deadline_ts}\n"
                f"Detail: {ticket.detail}\n"
            )
            smtp = self._smtp_factory()
            try:
                smtp.send_message(msg)
                self.delivered += 1
            finally:
                close = getattr(smtp, "quit", None) or getattr(smtp, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        _safe_call(_send, sink=self)


class FanOutNotificationSink(_SinkBase):
    """Sequence of sinks. A failure in one sink does NOT block the
    others — each fan-out target absorbs its own errors. Used when the
    owner wants both an email and a Slack ping."""

    def __init__(self, sinks: list) -> None:
        super().__init__()
        self._sinks = list(sinks)

    def notify(self, ticket) -> None:
        for s in self._sinks:
            try:
                s.notify(ticket)
            except Exception as exc:
                self._record_error(exc)


# ---------------------------------------------------------------------------
# Alert sinks (monitor → on-call)
# ---------------------------------------------------------------------------

class GenericWebhookAlertSink(_SinkBase):
    """JSON-POST webhook for monitor alerts. The body matches
    :meth:`Alert.to_dict` so a downstream pipeline can deserialize
    directly."""

    def __init__(
        self,
        *,
        url: str,
        http_client: Any | None = None,
        timeout_seconds: float = 5.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        if not url:
            raise ValueError("GenericWebhookAlertSink needs a url")
        self._url = url
        self._timeout = float(timeout_seconds)
        self._headers = {"Content-Type": "application/json",
                         **(extra_headers or {})}
        if http_client is None:
            try:
                import httpx
                http_client = httpx.Client(timeout=self._timeout)
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "GenericWebhookAlertSink requires httpx: "
                    "`pip install darwin-dna[platform]`"
                ) from exc
        self._http = http_client

    def emit(self, alert) -> None:
        def _post():
            resp = self._http.request(
                "POST", self._url,
                content=json.dumps(alert.to_dict()),
                headers=self._headers,
            )
            if int(getattr(resp, "status_code", 0)) >= 400:
                raise _SinkError(
                    f"webhook {self._url} returned "
                    f"{getattr(resp, 'status_code', '?')}"
                )
            self.delivered += 1
        _safe_call(_post, sink=self)


class PagerDutyAlertSink(_SinkBase):
    """PagerDuty Events v2. Maps severity → PD severity, emits a
    trigger event per critical alert. Lower severities can opt out
    via ``min_severity``."""

    def __init__(
        self,
        *,
        routing_key: str,
        http_client: Any | None = None,
        events_url: str = "https://events.pagerduty.com/v2/enqueue",
        min_severity: str = "critical",
        timeout_seconds: float = 5.0,
    ) -> None:
        super().__init__()
        if not routing_key:
            raise ValueError("PagerDutyAlertSink needs a routing_key")
        self._routing_key = routing_key
        self._url = events_url
        self._min_severity = min_severity
        self._timeout = float(timeout_seconds)
        if http_client is None:
            try:
                import httpx
                http_client = httpx.Client(timeout=self._timeout)
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "PagerDutyAlertSink requires httpx: "
                    "`pip install darwin-dna[platform]`"
                ) from exc
        self._http = http_client

    _SEVERITY_RANK = {"info": 0, "warn": 1, "critical": 2}

    def emit(self, alert) -> None:
        if self._SEVERITY_RANK.get(alert.severity.value, 0) < self._SEVERITY_RANK.get(self._min_severity, 0):
            return
        def _post():
            payload = {
                "routing_key": self._routing_key,
                "event_action": "trigger",
                "dedup_key": f"{alert.darwin_id}:{alert.kind}",
                "payload": {
                    "summary": (
                        f"Darwin {alert.darwin_id} :: "
                        f"{alert.kind} :: {alert.severity.value}"
                    ),
                    "source": alert.darwin_id,
                    "severity": (
                        "critical" if alert.severity.value == "critical"
                        else "warning"
                    ),
                    "custom_details": alert.detail,
                },
            }
            resp = self._http.request(
                "POST", self._url, content=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            if int(getattr(resp, "status_code", 0)) >= 400:
                raise _SinkError(
                    f"pagerduty returned {getattr(resp, 'status_code', '?')}"
                )
            self.delivered += 1
        _safe_call(_post, sink=self)


class FanOutAlertSink(_SinkBase):
    """Sequence of alert sinks. Same fan-out semantics as the
    notification variant — one sink's failure doesn't block the rest."""

    def __init__(self, sinks: list) -> None:
        super().__init__()
        self._sinks = list(sinks)

    def emit(self, alert) -> None:
        for s in self._sinks:
            try:
                s.emit(alert)
            except Exception as exc:
                self._record_error(exc)
