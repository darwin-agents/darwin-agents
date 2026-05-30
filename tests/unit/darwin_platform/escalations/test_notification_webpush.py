"""Tests for the Web Push notification sink (docs/engineering/22).

Coverage parallel to ``test_notification_fcm`` — same NotificationSink
Protocol, different transport (W3C Push + VAPID + ECE).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from darwin_dna.constitution import TicketClass
from darwin_platform.escalations.escalation import RoutedTicket, TicketStatus
from darwin_platform.escalations.notification_webpush import (
    PushSubscription,
    WebPushNotificationSink,
)


@dataclass
class _StubTransport:
    """Records every ``send`` call and replays scripted responses."""

    responses: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    raise_on_call: int | None = None

    def send(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if self.raise_on_call is not None and len(self.calls) > self.raise_on_call:
            raise RuntimeError("transport boom")
        if not self.responses:
            return {"status_code": 201, "body": "", "error": None}
        return self.responses.pop(0)


def _ticket(*, ticket_id="t-1", darwin_id="d-1", neuron="content.publisher",
            opened_ts=1_000.0, deadline_ts=2_000.0) -> RoutedTicket:
    return RoutedTicket(
        ticket_id=ticket_id, darwin_id=darwin_id,
        neuron=neuron, ticket_class=TicketClass.APPROVAL,
        opened_ts=opened_ts, deadline_ts=deadline_ts,
        status=TicketStatus.OPEN,
    )


def _make_sink(transport=None, resolver=None, purge=None, clock=None):
    return WebPushNotificationSink(
        vapid_private_key="-----BEGIN EC PRIVATE KEY-----\nstub\n-----END EC PRIVATE KEY-----",
        vapid_subject="mailto:ops@example.com",
        transport=transport or _StubTransport(),
        darwin_owner_resolver=resolver,
        subscription_purge=purge,
        clock=clock or (lambda: 100.0),
    )


def test_requires_vapid_private_key():
    with pytest.raises(ValueError, match="vapid_private_key"):
        WebPushNotificationSink(
            vapid_private_key="",
            vapid_subject="mailto:x@y.com",
            transport=_StubTransport(),
        )


def test_requires_vapid_subject():
    with pytest.raises(ValueError, match="vapid_subject"):
        WebPushNotificationSink(
            vapid_private_key="key",
            vapid_subject="",
            transport=_StubTransport(),
        )


def test_register_and_unregister_subscription():
    sink = _make_sink()
    sink.register_subscription(
        owner_subject="alice",
        endpoint="https://fcm.googleapis.com/wp/A",
        p256dh="p1", auth="a1",
    )
    sink.register_subscription(
        owner_subject="alice",
        endpoint="https://updates.push.services.mozilla.com/wpush/A",
        p256dh="p2", auth="a2",
    )
    sink.register_subscription(
        owner_subject="bob",
        endpoint="https://fcm.googleapis.com/wp/B",
        p256dh="p3", auth="a3",
    )
    alice_eps = {s.endpoint for s in sink.subscriptions_for("alice")}
    assert len(alice_eps) == 2
    assert sink.subscriptions_for("bob")[0].p256dh == "p3"

    removed = sink.unregister_subscription(
        owner_subject="alice",
        endpoint="https://fcm.googleapis.com/wp/A",
    )
    assert removed is True
    assert len(sink.subscriptions_for("alice")) == 1


def test_register_rejects_empty_fields():
    sink = _make_sink()
    with pytest.raises(ValueError):
        sink.register_subscription(
            owner_subject="", endpoint="e", p256dh="p", auth="a",
        )
    with pytest.raises(ValueError):
        sink.register_subscription(
            owner_subject="o", endpoint="", p256dh="p", auth="a",
        )


def test_notify_without_resolver_records_failure():
    transport = _StubTransport()
    sink = _make_sink(transport=transport)
    sink.register_subscription(
        owner_subject="alice",
        endpoint="https://fcm.googleapis.com/wp/X",
        p256dh="p", auth="a",
    )
    sink.notify(_ticket())
    assert sink.failure_count == 1
    assert sink.success_count == 0
    assert transport.calls == []  # no resolver → never reached transport
    rec = sink.history(limit=1)[0]
    assert rec.error == "no owner resolver"


def test_notify_owner_without_subscriptions_records_failure():
    transport = _StubTransport()
    sink = _make_sink(
        transport=transport,
        resolver=lambda _did: "alice",
    )
    sink.notify(_ticket())
    assert sink.failure_count == 1
    assert transport.calls == []


def test_notify_sends_one_push_per_subscription_on_success():
    transport = _StubTransport()
    sink = _make_sink(
        transport=transport,
        resolver=lambda _did: "alice",
    )
    sink.register_subscription(
        owner_subject="alice",
        endpoint="https://fcm.googleapis.com/wp/A",
        p256dh="p1", auth="a1",
    )
    sink.register_subscription(
        owner_subject="alice",
        endpoint="https://updates.push.services.mozilla.com/wpush/B",
        p256dh="p2", auth="a2",
    )
    sink.notify(_ticket())
    assert len(transport.calls) == 2
    assert sink.success_count == 2
    assert sink.failure_count == 0
    # VAPID aud claim should match each subscription's endpoint origin.
    auds = [c["vapid_claims"]["aud"] for c in transport.calls]
    assert auds == [
        "https://fcm.googleapis.com",
        "https://updates.push.services.mozilla.com",
    ]


def test_notify_4xx_counts_as_failure_no_raise():
    transport = _StubTransport(responses=[
        {"status_code": 400, "body": "bad", "error": None},
    ])
    sink = _make_sink(
        transport=transport,
        resolver=lambda _did: "alice",
    )
    sink.register_subscription(
        owner_subject="alice",
        endpoint="https://fcm.googleapis.com/wp/A",
        p256dh="p", auth="a",
    )
    sink.notify(_ticket())  # must NOT raise
    assert sink.failure_count == 1
    assert sink.success_count == 0


def test_notify_410_purges_subscription():
    purge_calls: list[tuple[str, str]] = []
    transport = _StubTransport(responses=[
        {"status_code": 410, "body": "", "error": None},
    ])
    sink = _make_sink(
        transport=transport,
        resolver=lambda _did: "alice",
        purge=lambda owner, ep: purge_calls.append((owner, ep)),
    )
    sink.register_subscription(
        owner_subject="alice",
        endpoint="https://fcm.googleapis.com/wp/A",
        p256dh="p", auth="a",
    )
    sink.notify(_ticket())
    # Sink should have auto-purged the stale subscription locally + fired
    # the persistence-side purge callback.
    assert sink.subscriptions_for("alice") == []
    assert purge_calls == [("alice", "https://fcm.googleapis.com/wp/A")]
    assert sink.purged_count == 1
    assert sink.failure_count == 1


def test_notify_transport_raises_recorded_no_crash():
    transport = _StubTransport(raise_on_call=0)
    sink = _make_sink(
        transport=transport,
        resolver=lambda _did: "alice",
    )
    sink.register_subscription(
        owner_subject="alice",
        endpoint="https://fcm.googleapis.com/wp/A",
        p256dh="p", auth="a",
    )
    sink.notify(_ticket())  # must NOT raise
    assert sink.failure_count == 1
    assert "transport raised" in sink.history()[-1].error


def test_load_subscriptions_bulk():
    sink = _make_sink()
    sink.load_subscriptions([
        ("alice", PushSubscription(
            endpoint="https://fcm.googleapis.com/wp/A",
            p256dh="p1", auth="a1", registered_at=42.0,
        )),
        ("alice", PushSubscription(
            endpoint="https://updates.push.services.mozilla.com/wpush/B",
            p256dh="p2", auth="a2", registered_at=43.0,
        )),
        ("bob", PushSubscription(
            endpoint="https://fcm.googleapis.com/wp/C",
            p256dh="p3", auth="a3", registered_at=44.0,
        )),
    ])
    assert len(sink.subscriptions_for("alice")) == 2
    assert len(sink.subscriptions_for("bob")) == 1


def test_stats_reports_counts():
    sink = _make_sink()
    sink.register_subscription(
        owner_subject="alice",
        endpoint="https://fcm.googleapis.com/wp/A",
        p256dh="p", auth="a",
    )
    s = sink.stats()
    assert s["owners_registered"] == 1
    assert s["subscriptions_registered"] == 1
    assert s["success_count"] == 0
    assert s["failure_count"] == 0
    assert s["purged_count"] == 0
