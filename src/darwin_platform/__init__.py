"""The Darwin platform control plane (docs/engineering/08).

NOT part of the genome. This is the SaaS-side supervisor that spawns,
supervises, and routes inputs to running Darwins. The genome is the
*organism*; this package is the *habitat*.

Organised as bounded contexts (each a subpackage):

* :mod:`darwin_platform.runtime` — supervisor, orchestrator, driver,
  persistence, leadership election.
* :mod:`darwin_platform.tenancy` — per-Darwin K8s + Vault namespace
  provisioning, vault admin, cross-pollination.
* :mod:`darwin_platform.payments` — billing, omnibus wallet, Stripe
  on-ramp, harvest scheduler, sanctions screening.
* :mod:`darwin_platform.security` — auth, KYC, signing service, key
  rotation, vault watermarking.
* :mod:`darwin_platform.observability` — tracing, monitoring, operator
  audit log, drift detector, Merkle anchoring, snapshot, notification
  + alert sinks.
* :mod:`darwin_platform.escalations` — escalation router + FCM push
  delivery.
* :mod:`darwin_platform.evolution` — Phase-1 sandboxed patch staging +
  eval runner.
* :mod:`darwin_platform.infra` — egress proxy, resource throttles,
  browser cache.
* :mod:`darwin_platform.adapters` — external system clients (wallet,
  vault, browser, sandbox, LLM, x402, fiat ramps, chain SDKs).
* :mod:`darwin_platform.k8s` — Kubernetes driver + manifest builders.

This top-level module re-exports the most commonly-consumed types
for convenience; new code should prefer importing directly from the
subpackage so the dependency graph stays visible.
"""

from __future__ import annotations

from .escalations import (
    EscalationRouter,
    LogNotificationSink,
    NotificationSink,
    RoutedTicket,
    TicketStatus,
)
from .observability import (
    Alert,
    AlertSink,
    DarwinSignals,
    LogAlertSink,
    Monitor,
    OperatorAuditEntry,
    OperatorAuditError,
    OperatorAuditLog,
    Quotas,
    Severity,
    run_detectors,
    snapshot,
    write_snapshot,
)
from .payments import BillingLedger, BillingPolicy, BillingService
from .runtime import (
    DarwinStatus,
    LeaderElection,
    LineageEdge,
    NoLeaderElection,
    PersistenceStore,
    PersistentBillingService,
    PersistentOperatorAuditLog,
    PersistentTokenStore,
    PostgresAdvisoryLockElection,
    QuarantineState,
    Supervisor,
    SupervisorError,
    UnknownDarwin,
)
from .security import (
    OWNER,
    PLATFORM_OPERATOR,
    AuthContext,
    InMemoryTokenStore,
    TokenStore,
    disabled_context,
)

__all__ = [
    "Alert",
    "AlertSink",
    "AuthContext",
    "BillingLedger",
    "BillingPolicy",
    "BillingService",
    "DarwinSignals",
    "DarwinStatus",
    "EscalationRouter",
    "InMemoryTokenStore",
    "LeaderElection",
    "LineageEdge",
    "LogAlertSink",
    "LogNotificationSink",
    "Monitor",
    "NoLeaderElection",
    "NotificationSink",
    "OWNER",
    "OperatorAuditEntry",
    "OperatorAuditError",
    "OperatorAuditLog",
    "PLATFORM_OPERATOR",
    "PersistenceStore",
    "PersistentBillingService",
    "PersistentOperatorAuditLog",
    "PersistentTokenStore",
    "PostgresAdvisoryLockElection",
    "QuarantineState",
    "Quotas",
    "RoutedTicket",
    "Severity",
    "Supervisor",
    "SupervisorError",
    "TicketStatus",
    "TokenStore",
    "UnknownDarwin",
    "disabled_context",
    "run_detectors",
    "snapshot",
    "write_snapshot",
]
