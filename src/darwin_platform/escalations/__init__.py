"""Escalations: owner-notification routing + delivery (docs/engineering/04, 22).

:class:`EscalationRouter` is the platform-side ledger of every ticket
the genome posts to its inbox. :class:`NotificationSink` is the
fan-out Protocol; the default :class:`LogNotificationSink` records
calls for tests. :class:`FCMNotificationSink` is the Phase-2 mobile
push delivery.
"""

from __future__ import annotations

from .escalation import (
    EscalationRouter,
    LogNotificationSink,
    NotificationSink,
    RoutedTicket,
    TicketStatus,
)
from .notification_fcm import (
    FCMDeliveryRecord,
    FCMNotificationSink,
    FCMTokenProvider,
    FCMTransport,
    StubFCMTokenProvider,
    StubFCMTransport,
)

__all__ = [
    "EscalationRouter",
    "FCMDeliveryRecord",
    "FCMNotificationSink",
    "FCMTokenProvider",
    "FCMTransport",
    "LogNotificationSink",
    "NotificationSink",
    "RoutedTicket",
    "StubFCMTokenProvider",
    "StubFCMTransport",
    "TicketStatus",
]
