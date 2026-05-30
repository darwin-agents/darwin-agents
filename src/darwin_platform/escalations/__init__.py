"""Escalations: owner-notification routing + delivery (docs/engineering/04, 22).

:class:`EscalationRouter` is the platform-side ledger of every ticket
the genome posts to its inbox. :class:`NotificationSink` is the
fan-out Protocol; the default :class:`LogNotificationSink` records
calls for tests. The mobile escalation companion (doc 22) ships as a PWA
and uses :class:`WebPushNotificationSink`; :class:`FCMNotificationSink`
remains available for deployers that want native FCM/APNs.
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
from .notification_webpush import (
    PushSubscription,
    PyWebPushTransport,
    WebPushDeliveryRecord,
    WebPushNotificationSink,
    WebPushTransport,
)

__all__ = [
    "EscalationRouter",
    "FCMDeliveryRecord",
    "FCMNotificationSink",
    "FCMTokenProvider",
    "FCMTransport",
    "LogNotificationSink",
    "NotificationSink",
    "PushSubscription",
    "PyWebPushTransport",
    "RoutedTicket",
    "StubFCMTokenProvider",
    "StubFCMTransport",
    "TicketStatus",
    "WebPushDeliveryRecord",
    "WebPushNotificationSink",
    "WebPushTransport",
]
