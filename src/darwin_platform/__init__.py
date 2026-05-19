"""The Darwin platform control plane (docs/engineering/08).

NOT part of the genome. This is the SaaS-side supervisor that spawns,
supervises, and routes inputs to running Darwins. The genome is the
*organism*; this package is the *habitat*.

First-pass scope (see the conversation that produced this): the Supervisor's
lifecycle API only. Each Darwin runs in-process as an asyncio task driven by
``darwin_fakes.make_platform``; the doc-08 K8s + microVM split is a later swap
behind the same Supervisor surface. Escalation router, anomaly detection,
billing, and the Next.js dashboard are out of scope here.
"""

from .escalation import (
    EscalationRouter,
    LogNotificationSink,
    NotificationSink,
    RoutedTicket,
    TicketStatus,
)
from .monitoring import (
    Alert,
    AlertSink,
    DarwinSignals,
    LogAlertSink,
    Monitor,
    Quotas,
    Severity,
    run_detectors,
)
from .snapshot import snapshot, write_snapshot
from .supervisor import (
    DarwinStatus,
    LineageEdge,
    Supervisor,
    SupervisorError,
    UnknownDarwin,
)

__all__ = [
    "Supervisor",
    "DarwinStatus",
    "LineageEdge",
    "SupervisorError",
    "UnknownDarwin",
    "EscalationRouter",
    "RoutedTicket",
    "TicketStatus",
    "NotificationSink",
    "LogNotificationSink",
    "Monitor",
    "Quotas",
    "Severity",
    "Alert",
    "AlertSink",
    "LogAlertSink",
    "DarwinSignals",
    "run_detectors",
    "snapshot",
    "write_snapshot",
]
