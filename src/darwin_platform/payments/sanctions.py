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
from typing import Any, Callable, Protocol, runtime_checkable


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


# -- transient-failure retry (docs/engineering/28: sanctions API gets
# 2 retries, fixed 2s) -----------------------------------------------------
def _request_with_retry(
    http: Any,
    *,
    method: str,
    url: str,
    max_retries: int,
    retry_delay: float,
    sleep: Callable[[float], None],
    **kwargs: Any,
) -> Any:
    """Issue an HTTP request, retrying transient failures (doc 28: the
    sanctions API gets 2 retries, fixed 2s). Transient = a raised exception
    OR a 5xx / 429 response. Returns the final response; re-raises the last
    exception only when *every* attempt errored, leaving the caller's
    fail-closed handler to take over."""
    last_exc: Exception | None = None
    last_resp: Any = None
    for attempt in range(1 + max(0, max_retries)):
        if attempt > 0:
            sleep(retry_delay)
        try:
            resp = http.request(method, url, **kwargs)
        except Exception as exc:  # noqa: BLE001 - transient; retry then fail closed
            last_exc = exc
            continue
        last_resp = resp
        status = int(getattr(resp, "status_code", 0))
        if status < 500 and status != 429:
            return resp  # success or a non-transient error — done
    if last_resp is not None:
        return last_resp  # every attempt returned a transient status
    raise last_exc if last_exc is not None else RuntimeError(
        "sanctions request failed with no response"
    )


class _RetryingScreener:
    """Mixin: a per-screener retry policy for the sanctions HTTP call
    (doc 28 — 2 retries, fixed 2s). Concrete screeners call ``_init_retry``
    in their ``__init__`` and ``_retried_request`` from ``check``."""

    _max_retries: int
    _retry_delay: float
    _sleep: Callable[[float], None]

    def _init_retry(self) -> None:
        self._max_retries = 2
        self._retry_delay = 2.0
        self._sleep = time.sleep

    def set_retry_policy(
        self,
        *,
        max_retries: int | None = None,
        retry_delay: float | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        """Tune the sanctions retry policy. ``max_retries=0`` disables it;
        ``sleep`` is the backoff function (injected by tests)."""
        if max_retries is not None:
            self._max_retries = max(0, int(max_retries))
        if retry_delay is not None:
            self._retry_delay = max(0.0, float(retry_delay))
        if sleep is not None:
            self._sleep = sleep

    def _retried_request(
        self, http: Any, *, method: str, url: str, **kwargs: Any
    ) -> Any:
        return _request_with_retry(
            http, method=method, url=url,
            max_retries=self._max_retries, retry_delay=self._retry_delay,
            sleep=self._sleep, **kwargs,
        )


class ChainalysisScreener(_RetryingScreener):
    """Real Chainalysis Address Screening client. POSTs the address to
    Chainalysis's Address Screening API; if the response classifies the
    address as ``severe`` or carries a sanctions exposure, returns
    ``allowed=False``.

    The injected ``http_client`` defaults to httpx.Client; production
    deployments inject one preconfigured with the API key header (or
    pass it via ``api_key`` and let the screener add it).

    Fail-closed: any HTTP error / parse error returns
    ``allowed=False, reason="screening_unavailable"`` so a misbehaving
    provider can't let a refused address through."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        http_client: Any | None = None,
        timeout_seconds: float = 5.0,
        risk_block_levels: tuple[str, ...] = ("severe", "high"),
    ) -> None:
        if not api_key:
            raise ValueError("ChainalysisScreener requires an api_key")
        self._key = api_key
        self._base = (base_url or "https://api.chainalysis.com").rstrip("/")
        self._risk_block = tuple(s.lower() for s in risk_block_levels)
        self._timeout = float(timeout_seconds)
        if http_client is None:
            try:
                import httpx
                http_client = httpx.Client(timeout=self._timeout)
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "ChainalysisScreener requires httpx: "
                    "`pip install darwin-dna[platform]`"
                ) from exc
        self._http = http_client
        self._init_retry()

    def check(self, address: str) -> ScreeningResult:
        import json as _json
        try:
            resp = self._retried_request(
                self._http,
                method="POST",
                url=f"{self._base}/api/risk/v2/entities",
                content=_json.dumps({"address": address}),
                headers={
                    "Token": self._key,
                    "Content-Type": "application/json",
                },
            )
            status = int(getattr(resp, "status_code", 0))
        except Exception:
            return ScreeningResult(
                allowed=False, reason="screening_unavailable",
                source="chainalysis",
            )
        if status != 200:
            return ScreeningResult(
                allowed=False, reason=f"chainalysis http {status}",
                source="chainalysis",
            )
        body = getattr(resp, "text", "") or "{}"
        try:
            data = _json.loads(body) if isinstance(body, str) else dict(body)
        except Exception:
            return ScreeningResult(
                allowed=False, reason="chainalysis bad json",
                source="chainalysis",
            )
        # Chainalysis Address Screening v2 surfaces a risk level + an
        # optional sanctions exposure. Block on severe/high risk OR any
        # sanctions exposure.
        risk = str(data.get("risk", "")).lower()
        sanctions = data.get("sanctions") or data.get("addressIdentifications") or []
        if isinstance(sanctions, list) and sanctions:
            return ScreeningResult(
                allowed=False,
                reason=f"chainalysis: {len(sanctions)} sanctions identifier(s)",
                source="chainalysis",
            )
        if risk in self._risk_block:
            return ScreeningResult(
                allowed=False,
                reason=f"chainalysis risk={risk}",
                source="chainalysis",
            )
        return ScreeningResult(allowed=True, source="chainalysis")


class TRMScreener(_RetryingScreener):
    """TRM Labs API screener. Same fail-closed posture as
    :class:`ChainalysisScreener`."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        http_client: Any | None = None,
        timeout_seconds: float = 5.0,
        risk_block_levels: tuple[str, ...] = ("severe", "high"),
    ) -> None:
        if not api_key:
            raise ValueError("TRMScreener requires an api_key")
        self._key = api_key
        self._base = (base_url or "https://api.trmlabs.com").rstrip("/")
        self._risk_block = tuple(s.lower() for s in risk_block_levels)
        self._timeout = float(timeout_seconds)
        if http_client is None:
            try:
                import httpx
                http_client = httpx.Client(timeout=self._timeout)
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "TRMScreener requires httpx: "
                    "`pip install darwin-dna[platform]`"
                ) from exc
        self._http = http_client
        self._init_retry()

    def check(self, address: str) -> ScreeningResult:
        import json as _json
        try:
            resp = self._retried_request(
                self._http,
                method="POST",
                url=f"{self._base}/public/v2/screening/addresses",
                content=_json.dumps([{"address": address}]),
                headers={
                    "Authorization": f"Basic {self._key}",
                    "Content-Type": "application/json",
                },
            )
            status = int(getattr(resp, "status_code", 0))
        except Exception:
            return ScreeningResult(
                allowed=False, reason="screening_unavailable",
                source="trm",
            )
        if status != 200:
            return ScreeningResult(
                allowed=False, reason=f"trm http {status}", source="trm",
            )
        body = getattr(resp, "text", "") or "[]"
        try:
            data = _json.loads(body) if isinstance(body, str) else list(body)
        except Exception:
            return ScreeningResult(
                allowed=False, reason="trm bad json", source="trm",
            )
        if not isinstance(data, list) or not data:
            return ScreeningResult(
                allowed=False, reason="trm empty response", source="trm",
            )
        first = data[0]
        if not isinstance(first, dict):
            return ScreeningResult(
                allowed=False, reason="trm response shape", source="trm",
            )
        risk = str(first.get("addressRiskIndicators", [{}])[0].get(
            "categoryRiskScoreLevel", ""
        )).lower() if first.get("addressRiskIndicators") else ""
        sanctions = first.get("entities", [])
        if sanctions:
            for ent in sanctions:
                if isinstance(ent, dict) and (
                    "sanction" in str(ent.get("category", "")).lower()
                ):
                    return ScreeningResult(
                        allowed=False,
                        reason=f"trm: entity {ent.get('entity')}",
                        source="trm",
                    )
        if risk in self._risk_block:
            return ScreeningResult(
                allowed=False, reason=f"trm risk={risk}", source="trm",
            )
        return ScreeningResult(allowed=True, source="trm")


@dataclass
class SanctionsRefusal:
    """Captured refusal — useful for the supervisor's audit + dashboards.
    Holds only the recipient and reason; never the amount or memo (those
    can leak business intent into a compliance log)."""

    address: str
    reason: str
    source: str
    ts: float = field(default_factory=time.time)
