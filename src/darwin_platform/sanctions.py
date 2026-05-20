"""Sanctions / OFAC screening (docs/engineering/17 §"Compliance").

Doc 17: "screen the recipient against the OFAC SDN list before every
transfer." This module ships the platform-side machinery; the genome's
``treasury_policy`` doesn't know about it (the genome only sees a wallet
refusal).

Architecture:

- :class:`SanctionsScreener` — Protocol. ``check(address) -> ScreeningResult``.
- :class:`LocalDenylistScreener` — default; offline list of denied addresses
  + prefixes. Production wires Chainalysis / TRM Labs behind the same
  Protocol.
- :class:`CachedScreener` — caches results so a hot-loop of identical
  recipient addresses doesn't hammer the provider.
- :class:`AllowAllScreener` — explicit opt-out, useful for tests.
- :class:`ChainalysisScreener` — stub; raises until a real client is wired.

Wallet integration: :class:`darwin_platform.adapters.wallet.RealWallet` and
:class:`darwin_fakes.fake_wallet.FakeWallet` both accept an optional
screener and call it before any outbound transfer. The Supervisor holds
one screener per fleet and installs it on every wallet at spawn time.

Failure mode: a screener refusal surfaces as the same ``WalletError`` the
genome already handles for a refused payment. The audit log records the
*reason* (sanctions:address-on-denylist) rather than the value, so a
compliance review can replay the decision without leaking the recipient.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable


@dataclass(frozen=True)
class ScreeningResult:
    """The screener's verdict for one address."""

    allowed: bool
    reason: str | None = None
    source: str = "local"  # "local" | "chainalysis" | "trm" | "bypass"


@runtime_checkable
class SanctionsScreener(Protocol):
    """Single method. ``check(address) -> ScreeningResult``. Implementations
    must NEVER raise on the happy path — a backend failure should surface
    as ``ScreeningResult(allowed=False, reason="screening_unavailable")``
    so the wallet fails closed."""

    def check(self, address: str) -> ScreeningResult: ...


class AllowAllScreener:
    """Explicit bypass. Useful for tests and for development environments
    where running the real provider isn't worth the latency. NEVER use
    this in production — the audit trail will record ``source='bypass'``
    so the choice is visible at compliance review."""

    def check(self, address: str) -> ScreeningResult:
        del address
        return ScreeningResult(allowed=True, source="bypass")


class LocalDenylistScreener:
    """Default offline screener. Loaded from a denylist set + optional
    prefix list. The OFAC SDN list of USDC addresses fits comfortably
    in this format (a few thousand entries); production deployments load
    a fresh dump on supervisor startup.

    Both inputs are normalised to lowercase + stripped so an attacker can't
    bypass via casing or whitespace tricks."""

    def __init__(
        self,
        *,
        denylist: set[str] | None = None,
        denied_prefixes: tuple[str, ...] = (),
        symbolic_allow: tuple[str, ...] = (
            "platform.fee", "child.wallet", "platform.gateway",
        ),
    ) -> None:
        self._deny = {self._norm(a) for a in (denylist or set())}
        self._prefixes = tuple(self._norm(p) for p in denied_prefixes if p)
        # Symbolic recipients the adapter uses internally for accounting
        # (platform fee, child wallet); these never hit a real address so
        # they're allowed unconditionally. Lower-cased; tests can extend.
        self._symbolic = {self._norm(s) for s in symbolic_allow}

    @staticmethod
    def _norm(s: str) -> str:
        return str(s).strip().lower()

    def check(self, address: str) -> ScreeningResult:
        a = self._norm(address)
        if a in self._symbolic:
            return ScreeningResult(allowed=True, source="local")
        if a in self._deny:
            return ScreeningResult(
                allowed=False,
                reason="address on local denylist",
                source="local",
            )
        for p in self._prefixes:
            if p and a.startswith(p):
                return ScreeningResult(
                    allowed=False,
                    reason=f"address prefix {p!r} on denied list",
                    source="local",
                )
        return ScreeningResult(allowed=True, source="local")


class CachedScreener:
    """Wraps any screener; caches each address's verdict for ``ttl_seconds``.
    A typical platform sees the same recipients again and again (payment
    rails, ad networks, exchanges), so caching saves real money on
    provider calls."""

    def __init__(
        self,
        inner: SanctionsScreener,
        *,
        ttl_seconds: float = 3600.0,
        clock: Callable[[], float] = time.time,
        max_entries: int = 1024,
    ) -> None:
        self._inner = inner
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._max = int(max_entries)
        self._cache: dict[str, tuple[float, ScreeningResult]] = {}

    def check(self, address: str) -> ScreeningResult:
        now = self._clock()
        hit = self._cache.get(address)
        if hit is not None and (now - hit[0]) < self._ttl:
            return hit[1]
        result = self._inner.check(address)
        if len(self._cache) >= self._max:
            # Cheap eviction: drop the oldest.
            oldest = min(self._cache.items(), key=lambda kv: kv[1][0])[0]
            self._cache.pop(oldest, None)
        self._cache[address] = (now, result)
        return result


class ChainalysisScreener:
    """Stub for the Chainalysis Reactor API. The real client wires to
    https://api.chainalysis.com and returns risk categories; here we keep
    the Protocol shape ready and raise loudly if the platform tries to
    use it without wiring."""

    def __init__(self, *, api_key: str, base_url: str | None = None) -> None:
        self._key = api_key
        self._base = base_url or "https://api.chainalysis.com"

    def check(self, address: str) -> ScreeningResult:  # pragma: no cover
        raise NotImplementedError(
            "ChainalysisScreener: wire to the real Chainalysis Reactor "
            "client; this is a stub. Pass a custom SanctionsScreener "
            "with the right API client in production."
        )


@dataclass
class SanctionsRefusal:
    """Captured refusal — useful for the supervisor's audit + dashboards.
    Holds only the recipient and reason; never the amount or memo (those
    can leak business intent into a compliance log)."""

    address: str
    reason: str
    source: str
    ts: float = field(default_factory=time.time)
