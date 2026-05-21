"""Observability: traces, metrics, audit, alerts (docs/engineering/08, 22).

:mod:`tracing` is the OpenTelemetry span helper (NoOp when SDK absent).
:mod:`monitoring` defines the Quotas + per-signal detectors that fire
:class:`Alert` rows into an :class:`AlertSink`. :mod:`operator_audit`
is the hash-chained durable log every supervisor mutation lands on.
:mod:`drift_detector` watches per-Darwin behaviour for 2σ deviations.
:mod:`merkle_anchor` publishes a daily fleet-wide root for
third-party tamper-evidence. :mod:`snapshot` is the ops-side
JSON dump of supervisor state. :mod:`sinks` ships notification +
alert delivery integrations (Slack, PagerDuty, webhooks, SMTP).
"""

from __future__ import annotations

from .drift_detector import (
    DailyProfile,
    DriftAlert,
    DriftDetector,
    DriftRollingState,
    DriftSeverity,
)
from .merkle_anchor import (
    AnchorBackend,
    ChainAnchorBackend,
    DarwinDailyRoot,
    FleetAnchor,
    MerkleAnchorPublisher,
    StubAnchorBackend,
    merkle_root,
)
from .monitoring import (
    Alert,
    AlertSink,
    DarwinSignals,
    LogAlertSink,
    Monitor,
    Quotas,
    Severity,
    detect_escalation_rate,
    detect_failure_rate,
    detect_heartbeat_health,
    detect_spend_velocity,
    detect_spread_spend,
    run_detectors,
)
from .operator_audit import (
    OperatorAuditEntry,
    OperatorAuditError,
    OperatorAuditLog,
    to_dicts,
)
from .sinks import (
    FanOutAlertSink,
    FanOutNotificationSink,
    GenericWebhookAlertSink,
    GenericWebhookNotificationSink,
    PagerDutyAlertSink,
    SMTPEmailNotificationSink,
    SlackWebhookNotificationSink,
)
from .snapshot import snapshot, write_snapshot
from .tracing import (
    annotate_span_with_runtime,
    get_tracer,
    is_otel_available,
    span,
)

__all__ = [
    "Alert",
    "AlertSink",
    "AnchorBackend",
    "ChainAnchorBackend",
    "DailyProfile",
    "DarwinDailyRoot",
    "DarwinSignals",
    "DriftAlert",
    "DriftDetector",
    "DriftRollingState",
    "DriftSeverity",
    "FanOutAlertSink",
    "FanOutNotificationSink",
    "FleetAnchor",
    "GenericWebhookAlertSink",
    "GenericWebhookNotificationSink",
    "LogAlertSink",
    "MerkleAnchorPublisher",
    "Monitor",
    "OperatorAuditEntry",
    "OperatorAuditError",
    "OperatorAuditLog",
    "PagerDutyAlertSink",
    "Quotas",
    "SMTPEmailNotificationSink",
    "Severity",
    "SlackWebhookNotificationSink",
    "StubAnchorBackend",
    "annotate_span_with_runtime",
    "detect_escalation_rate",
    "detect_failure_rate",
    "detect_heartbeat_health",
    "detect_spend_velocity",
    "detect_spread_spend",
    "get_tracer",
    "is_otel_available",
    "merkle_root",
    "run_detectors",
    "snapshot",
    "span",
    "to_dicts",
    "write_snapshot",
]
