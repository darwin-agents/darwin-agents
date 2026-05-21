"""Infra: substrate-level resource controls (docs/engineering/09, 15, 22).

:mod:`throttle` ships per-Darwin daily call caps on non-LLM resources
(sandbox, browser, egress). :mod:`egress_proxy` is the cluster-wide
outbound network gateway: allowlist-only by default + Phase-2
per-Darwin bandwidth quotas. :mod:`browser_cache` records every
browser session, attributes cost, and promotes signatures with N
successes to a cheap replay path.
"""

from __future__ import annotations

from .browser_cache import (
    BrowserCacheService,
    BrowserSessionRecord,
    CachedBrowserSubstrate,
)
from .egress_proxy import (
    AccessLogEntry,
    Allowlist,
    AllowlistEntry,
    BandwidthQuota,
)
from .throttle import (
    THROTTLE_KIND_BROWSER,
    THROTTLE_KIND_EGRESS,
    THROTTLE_KIND_SANDBOX,
    THROTTLE_KINDS,
    ResourceThrottle,
    ThrottleError,
    ThrottleService,
    ThrottledBrowserSubstrate,
    ThrottledEgressClient,
    ThrottledSandboxSubstrate,
)

__all__ = [
    "AccessLogEntry",
    "Allowlist",
    "AllowlistEntry",
    "BandwidthQuota",
    "BrowserCacheService",
    "BrowserSessionRecord",
    "CachedBrowserSubstrate",
    "ResourceThrottle",
    "THROTTLE_KINDS",
    "THROTTLE_KIND_BROWSER",
    "THROTTLE_KIND_EGRESS",
    "THROTTLE_KIND_SANDBOX",
    "ThrottleError",
    "ThrottleService",
    "ThrottledBrowserSubstrate",
    "ThrottledEgressClient",
    "ThrottledSandboxSubstrate",
]
