"""Runtime: per-Darwin lifecycle ownership (docs/engineering/08).

The supervisor is the single writer for lifecycle transitions; the
orchestrator drives the inner heartbeat loop inside a per-Darwin
sandbox; the driver Protocol lets us swap between in-process asyncio
(dev / sim) and K8s (production). Persistence + leadership are the
durability + HA primitives every runtime mode shares.
"""

from __future__ import annotations

from .driver import (
    DarwinDriver,
    DarwinNotFound,
    DarwinSnapshot,
    DarwinSpec,
    DriverError,
)
from .leadership import (
    LeaderElection,
    NoLeaderElection,
    PostgresAdvisoryLockElection,
)
from .persistence import (
    PersistenceStore,
    PersistentBillingService,
    PersistentOperatorAuditLog,
    PersistentTokenStore,
)
from .supervisor import (
    DarwinStatus,
    LineageEdge,
    LineageRegistry,
    QuarantineState,
    Supervisor,
    SupervisorError,
    UnknownDarwin,
)

__all__ = [
    "DarwinDriver",
    "DarwinNotFound",
    "DarwinSnapshot",
    "DarwinSpec",
    "DriverError",
    "DarwinStatus",
    "LeaderElection",
    "LineageEdge",
    "LineageRegistry",
    "NoLeaderElection",
    "PersistenceStore",
    "PersistentBillingService",
    "PersistentOperatorAuditLog",
    "PersistentTokenStore",
    "PostgresAdvisoryLockElection",
    "QuarantineState",
    "Supervisor",
    "SupervisorError",
    "UnknownDarwin",
]
