"""Tests for FCM notification sink (docs/engineering/22).

Coverage:
- Construction requires project_id.
- register/unregister token round-trip; tokens_for filtered by owner.
- notify() with no resolver still records a failure (no crash).
- notify() with a resolver sends one push per registered token.
- HTTP 4xx/5xx counts as failure but does not raise.
- Transport exception counts as failure but does not raise.
- Owner without tokens: recorded failure, no crash.
"""

from __future__ import annotations

import time

import pytest

from darwin_platform.escalation import (
    LogNotificationSink,  # for shape comparison
    RoutedTicket,
    TicketStatus,
)
from darwin_dna.constitution import TicketClass
from darwin_platform.notification_fcm import (
    FCMNotificationSink,
    StubFCMTokenProvider,
    StubFCMTransport,
)


def _ticket(*, ticket_id="t-1", darwin_id="d-1", neuron="content.publisher",
            opened_ts=1_000.0, deadline_ts=2_000.0) -> RoutedTicket:
    return RoutedTicket(
        ticket_id=ticket_id, darwin_id=darwin_id,
        neuron=neuron, ticket_class=TicketClass.APPROVAL,
        opened_ts=opened_ts, deadline_ts=deadline_ts,
        status=TicketStatus.OPEN,
    )


def test_requires_project_id():
    with pytest.raises(ValueError, match="project_id"):
        FCMNotificationSink(
            project_id="", token_provider=StubFCMTokenProvider(),
            transport=StubFCMTransport(),
        )


def test_register_and_unregister_tokens():
    sink = FCMNotificationSink(
        project_id="p", token_provider=StubFCMTokenProvider(),
        transport=StubFCMTransport(),
    )
    sink.register_token(owner_subject="alice", device_token="tok-a1")
    sink.register_token(owner_subject="alice", device_token="tok-a2")
    sink.register_token(owner_subject="bob", device_token="tok-b1")
    assert set(sink.tokens_for("alice")) == {"tok-a1", "tok-a2"}
    assert sink.tokens_for("bob") == ["tok-b1"]
    sink.unregister_token(owner_subject="alice", device_token="tok-a1")
    assert sink.tokens_for("alice") == ["tok-a2"]


def test_register_rejects_empty_subject_or_token():
    sink = FCMNotificationSink(
        project_id="p", token_provider=StubFCMTokenProvider(),
        transport=StubFCMTransport(),
    )
    with pytest.raises(ValueError):
        sink.register_token(owner_subject="", device_token="t")
    with pytest.raises(ValueError):
        sink.register_token(owner_subject="alice", device_token="")


def test_notify_without_resolver_records_failure():
    transport = StubFCMTransport()
    sink = FCMNotificationSink(
        project_id="p", token_provider=StubFCMTokenProvider(),
        transport=transport,
    )
    sink.register_token(owner_subject="alice", device_token="tok-a1")
    sink.notify(_ticket())
    assert transport.requests == []  # never sent — no resolver
    assert sink.failure_count == 1
    assert sink.success_count == 0


def test_notify_sends_one_push_per_token_with_resolver():
    transport = StubFCMTransport()
    sink = FCMNotificationSink(
        project_id="p", token_provider=StubFCMTokenProvider(),
        transport=transport,
        darwin_owner_resolver=lambda did: "alice" if did == "d-1" else None,
    )
    sink.register_token(owner_subject="alice", device_token="tok-1")
    sink.register_token(owner_subject="alice", device_token="tok-2")
    sink.notify(_ticket(darwin_id="d-1"))
    assert sink.success_count == 2
    assert sink.failure_count == 0
    assert len(transport.requests) == 2
    tokens_sent = [r["json_body"]["message"]["token"] for r in transport.requests]
    assert set(tokens_sent) == {"tok-1", "tok-2"}


def test_notify_4xx_counts_as_failure_without_raising():
    transport = StubFCMTransport(
        status_code=400,
        body={"error": {"code": 400, "status": "INVALID_ARGUMENT"}},
    )
    sink = FCMNotificationSink(
        project_id="p", token_provider=StubFCMTokenProvider(),
        transport=transport,
        darwin_owner_resolver=lambda did: "alice",
    )
    sink.register_token(owner_subject="alice", device_token="tok-1")
    sink.notify(_ticket())  # must not raise
    assert sink.failure_count == 1
    assert sink.success_count == 0


def test_notify_transport_exception_counts_as_failure():
    transport = StubFCMTransport(raises=ConnectionError("network down"))
    sink = FCMNotificationSink(
        project_id="p", token_provider=StubFCMTokenProvider(),
        transport=transport,
        darwin_owner_resolver=lambda did: "alice",
    )
    sink.register_token(owner_subject="alice", device_token="tok-1")
    sink.notify(_ticket())  # must not raise
    assert sink.failure_count == 1
    history = sink.history(limit=1)
    assert "network down" in history[0].error


def test_notify_owner_without_tokens_records_failure():
    sink = FCMNotificationSink(
        project_id="p", token_provider=StubFCMTokenProvider(),
        transport=StubFCMTransport(),
        darwin_owner_resolver=lambda did: "alice",  # alice has no tokens
    )
    sink.notify(_ticket())
    assert sink.failure_count == 1
    assert sink.success_count == 0


def test_stats_snapshot_shape():
    sink = FCMNotificationSink(
        project_id="p", token_provider=StubFCMTokenProvider(),
        transport=StubFCMTransport(),
    )
    sink.register_token(owner_subject="alice", device_token="tok-1")
    snap = sink.stats()
    assert snap["owners_registered"] == 1
    assert snap["tokens_registered"] == 1
    assert snap["success_count"] == 0
    assert snap["failure_count"] == 0
