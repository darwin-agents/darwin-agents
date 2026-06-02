"""Platform egress proxy (docs/engineering/08 §"Outbound network filter",
docs/overview/07).

Sits in the ``platform-data`` namespace; every per-Darwin orchestrator
pod's outbound traffic is constrained by NetworkPolicy to flow through
this service. The proxy:

- Refuses requests to destinations not on the allowlist (the
  "blocklist by default" stance — the doc-08 wording matters: only
  *sanctioned* destinations get through).
- Logs every attempted request (allowed + denied) so a compliance
  reviewer can prove what each Darwin reached.
- Bills nothing — the genome's wallet handles spend; this is enforcement,
  not metering.

Two modes, switchable by the destination scheme:

- ``http`` / ``https`` URLs: proxied as a *gateway* — the client POSTs
  the request envelope (method, URL, headers, body) to ``/proxy`` and
  receives the response. This is the right shape for the genome's
  substrates (LiteLLM, Browserbase, Coinbase) which call typed clients
  rather than raw HTTP.
- Plain TCP CONNECT (for protocols not over HTTPS) is **not** supported
  in this slice — every Darwin substrate is HTTPS-shaped today.

Run::

    python -m darwin_platform.egress_proxy serve \\
        --port 8080 \\
        --allowlist /etc/egress/allowlist.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class AllowlistEntry:
    """One sanctioned outbound destination.

    Matching is host-suffix based ("*.googleapis.com" matches any
    subdomain). Ports must match exactly; ``None`` means any port. The
    HTTP method allowlist (``methods``) bounds what the genome can do
    against the destination (a read-only API gets GET only)."""

    host_suffix: str  # e.g. "api.openai.com" or ".googleapis.com"
    port: int | None = None
    schemes: tuple[str, ...] = ("https",)
    methods: tuple[str, ...] = ("GET", "POST")
    note: str = ""

    def matches(self, scheme: str, host: str, port: int | None) -> bool:
        if scheme not in self.schemes:
            return False
        if self.port is not None and port != self.port:
            return False
        host_l = host.lower()
        suffix = self.host_suffix.lower()
        if suffix.startswith("."):
            return host_l.endswith(suffix) or host_l == suffix[1:]
        return host_l == suffix or host_l.endswith("." + suffix)


@dataclass
class AccessLogEntry:
    ts: float
    darwin_id: str | None
    method: str
    url: str
    status: int  # HTTP status or 0 if denied pre-flight
    bytes_in: int
    bytes_out: int
    denied_reason: str | None
    matched_rule: str | None


@dataclass
class Allowlist:
    """In-memory ruleset. Reloadable for ops convenience — write the
    JSON file, send SIGHUP, the proxy rereads."""

    entries: list[AllowlistEntry] = field(default_factory=list)

    @classmethod
    def from_path(cls, path: Path) -> "Allowlist":
        raw = json.loads(path.read_text())
        out = cls()
        for row in raw.get("entries", []):
            out.entries.append(
                AllowlistEntry(
                    host_suffix=row["host_suffix"],
                    port=row.get("port"),
                    schemes=tuple(row.get("schemes", ["https"])),
                    methods=tuple(row.get("methods", ["GET", "POST"])),
                    note=row.get("note", ""),
                )
            )
        return out

    def check(
        self, *, scheme: str, host: str, port: int | None, method: str,
    ) -> tuple[bool, AllowlistEntry | None, str | None]:
        for e in self.entries:
            if e.matches(scheme, host, port):
                if method.upper() in e.methods:
                    return True, e, None
                return False, e, f"method {method!r} not allowed for {e.host_suffix!r}"
        return False, None, f"destination {scheme}://{host}:{port or '*'} not allowlisted"


class _Audit:
    """Bounded in-memory ring of access log entries. Production wires a
    Postgres write-through; the in-memory ring is what the operator
    queries via ``/access-log``."""

    def __init__(self, *, capacity: int = 10_000) -> None:
        self._rows: deque[AccessLogEntry] = deque(maxlen=capacity)

    def append(self, row: AccessLogEntry) -> None:
        self._rows.append(row)

    def snapshot(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return [
            {
                "ts": r.ts,
                "darwin_id": r.darwin_id,
                "method": r.method,
                "url": r.url,
                "status": r.status,
                "bytes_in": r.bytes_in,
                "bytes_out": r.bytes_out,
                "denied_reason": r.denied_reason,
                "matched_rule": r.matched_rule,
            }
            for r in list(self._rows)[-limit:]
        ]


# ---------------------------------------------------------------------------
# Bandwidth quota (Phase 2 §"The egress proxy")
# ---------------------------------------------------------------------------
@dataclass
class BandwidthQuota:
    """Per-Darwin daily bandwidth ceiling.

    Doc 22's defaults: 10 GB/day soft, 2× burst (20 GB) hard. The
    proxy refuses (HTTP 429) requests that would push a Darwin above
    the hard ceiling for the current UTC day. The soft ceiling fires
    a WARN that the supervisor's monitor consumes (the proxy only
    enforces the hard cap; informational throttling lives upstream).

    Accounting is bytes_in + bytes_out: a Darwin paying for a 1 GB
    LLM response should hit the cap just as fast as one fetching a
    1 GB page. Daily rollover is UTC.
    """

    soft_bytes_per_day: int = 10 * 1024 * 1024 * 1024     # 10 GB
    hard_bytes_per_day: int = 20 * 1024 * 1024 * 1024     # 20 GB (2× burst)
    _today: str = ""
    _usage: dict[str, int] = field(default_factory=dict)
    _warned: set[str] = field(default_factory=set)

    def _day(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _roll_if_new_day(self) -> None:
        d = self._day()
        if d != self._today:
            self._today = d
            self._usage.clear()
            self._warned.clear()

    def used_today(self, darwin_id: str) -> int:
        self._roll_if_new_day()
        return int(self._usage.get(darwin_id, 0))

    def remaining_today(self, darwin_id: str) -> int:
        return max(0, self.hard_bytes_per_day - self.used_today(darwin_id))

    def check_and_charge(
        self, *, darwin_id: str | None, bytes_used: int,
    ) -> tuple[bool, str | None, bool]:
        """Record ``bytes_used`` against ``darwin_id``. Returns
        ``(allowed, denied_reason, soft_breach_first_time)``. Anonymous
        traffic (no darwin_id) is exempt from per-Darwin caps but
        counted under the synthetic ``"__anonymous__"`` bucket for
        ops visibility.

        ``soft_breach_first_time`` is True only on the first call after
        the soft cap is crossed for the day — the caller fires a single
        WARN per Darwin per day rather than spamming."""
        self._roll_if_new_day()
        key = darwin_id or "__anonymous__"
        current = int(self._usage.get(key, 0))
        projected = current + max(0, int(bytes_used))
        if darwin_id is not None and projected > self.hard_bytes_per_day:
            return (
                False,
                f"bandwidth cap: {projected} bytes > "
                f"{self.hard_bytes_per_day} bytes/day",
                False,
            )
        self._usage[key] = projected
        soft_first = False
        if (
            darwin_id is not None
            and projected > self.soft_bytes_per_day
            and key not in self._warned
        ):
            self._warned.add(key)
            soft_first = True
        return True, None, soft_first

    def snapshot(self) -> dict[str, Any]:
        self._roll_if_new_day()
        return {
            "day": self._today,
            "soft_bytes_per_day": self.soft_bytes_per_day,
            "hard_bytes_per_day": self.hard_bytes_per_day,
            "per_darwin_usage": dict(self._usage),
            "warned_soft_breach": sorted(self._warned),
        }


# ---------------------------------------------------------------------------
# Per-Darwin egress revocation (docs/engineering/08, 09 §kill path, 24)
# ---------------------------------------------------------------------------
class RevocationRegistry:
    """Per-Darwin outbound kill-switch.

    When the supervisor kills or quarantines a Darwin it revokes the Darwin
    here; the proxy then refuses ALL of that Darwin's outbound traffic
    regardless of the allowlist. This is the *parallel egress cut* that bounds
    a Darwin which has stopped polling its own kill flag — the load-bearing
    mitigation behind the "<5s effective halt" claim (docs 08/09/24). Without
    it, an unresponsive orchestrator could keep reaching the network until its
    next in-process poll.
    """

    def __init__(self) -> None:
        self._revoked: set[str] = set()

    def revoke(self, darwin_id: str) -> None:
        if darwin_id:
            self._revoked.add(darwin_id)

    def restore(self, darwin_id: str) -> None:
        self._revoked.discard(darwin_id)

    def is_revoked(self, darwin_id: str | None) -> bool:
        return bool(darwin_id) and darwin_id in self._revoked

    def snapshot(self) -> list[str]:
        return sorted(self._revoked)


class HttpEgressRevoker:
    """Supervisor-side client for the egress proxy's kill-switch.

    ``Supervisor`` calls ``revoke(darwin_id)`` on kill/quarantine; this POSTs
    ``/revoke`` to the platform egress proxy so the Darwin's outbound traffic
    is cut at the choke-point. Synchronous + short-timeout — the supervisor
    runs it off its event loop and never blocks the kill on it.
    """

    def __init__(self, *, base_url: str, timeout_s: float = 3.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = float(timeout_s)

    def revoke(self, darwin_id: str) -> None:
        import httpx

        resp = httpx.post(
            f"{self._base}/revoke",
            json={"darwin_id": darwin_id},
            timeout=self._timeout,
        )
        resp.raise_for_status()

    def restore(self, darwin_id: str) -> None:
        import httpx

        resp = httpx.post(
            f"{self._base}/unrevoke",
            json={"darwin_id": darwin_id},
            timeout=self._timeout,
        )
        resp.raise_for_status()


def _build_app(
    allowlist: Allowlist,
    audit: _Audit,
    *,
    clock=time.time,
    bandwidth_quota: BandwidthQuota | None = None,
    revocations: "RevocationRegistry | None" = None,
):
    try:
        from fastapi import Body, FastAPI, Header, HTTPException
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "egress_proxy requires the [platform] extra (FastAPI + httpx)"
        ) from exc

    app = FastAPI(title="darwin-egress-proxy")
    # Bandwidth quota is optional so the existing single-tenant dev
    # deployment continues to work — only Phase 2 multi-tenant mode
    # constructs the proxy with a quota.
    quota = bandwidth_quota
    revoked = revocations

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.post("/revoke")
    async def revoke(body: dict = Body(...)) -> dict:
        """Cut a Darwin's egress immediately (kill / quarantine). Idempotent."""
        if revoked is None:
            raise HTTPException(404, "revocation not enabled")
        did = str(body.get("darwin_id", "")).strip()
        if not did:
            raise HTTPException(400, "darwin_id required")
        revoked.revoke(did)
        return {"revoked": did}

    @app.post("/unrevoke")
    async def unrevoke(body: dict = Body(...)) -> dict:
        """Restore a Darwin's egress (e.g. quarantine cleared). Idempotent."""
        if revoked is None:
            raise HTTPException(404, "revocation not enabled")
        did = str(body.get("darwin_id", "")).strip()
        if not did:
            raise HTTPException(400, "darwin_id required")
        revoked.restore(did)
        return {"unrevoked": did}

    @app.get("/revoked")
    async def revoked_list() -> dict:
        return {
            "enabled": revoked is not None,
            "revoked": revoked.snapshot() if revoked is not None else [],
        }

    @app.get("/access-log")
    async def access_log(limit: int = 200) -> list[dict[str, Any]]:
        return audit.snapshot(limit=min(max(limit, 1), 2000))

    @app.get("/bandwidth")
    async def bandwidth() -> dict:
        if quota is None:
            return {"enabled": False}
        snap = quota.snapshot()
        snap["enabled"] = True
        return snap

    @app.post("/proxy")
    async def proxy(
        body: dict = Body(...),
        x_darwin_id: str | None = Header(default=None),
    ) -> dict:
        """Envelope shape::

            { "method": "POST",
              "url": "https://api.openai.com/v1/chat/completions",
              "headers": {...}, "body_b64": "..." }

        Returns::

            { "status": 200,
              "headers": {...}, "body_b64": "..." }
        """
        method = str(body.get("method", "")).upper()
        url = str(body.get("url", ""))
        if not method or not url:
            raise HTTPException(400, "method + url are required")
        # Egress kill-switch: a revoked (killed/quarantined) Darwin is cut off
        # at the choke-point regardless of the allowlist, in parallel with the
        # in-process kill flag (docs 08/09/24).
        if revoked is not None and revoked.is_revoked(x_darwin_id):
            audit.append(AccessLogEntry(
                ts=clock(), darwin_id=x_darwin_id, method=method, url=url,
                status=0, bytes_in=0, bytes_out=0,
                denied_reason="darwin revoked (killed/quarantined)",
                matched_rule=None,
            ))
            raise HTTPException(403, "darwin revoked: egress cut by kill/quarantine")
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
        port = parsed.port
        ok, entry, reason = allowlist.check(
            scheme=scheme, host=host, port=port, method=method,
        )
        if not ok:
            audit.append(AccessLogEntry(
                ts=clock(), darwin_id=x_darwin_id, method=method, url=url,
                status=0, bytes_in=0, bytes_out=0,
                denied_reason=reason, matched_rule=None,
            ))
            raise HTTPException(403, reason or "denied")

        # Pre-flight bandwidth check (Phase 2 §"The egress proxy"). We
        # refuse the request OUTRIGHT when the Darwin is already past
        # the hard cap, rather than letting the round-trip happen and
        # then refusing the response. Pre-flight uses the request body
        # size as a lower bound; the response charges the real
        # bytes_in + bytes_out after.
        headers = {str(k): str(v) for k, v in (body.get("headers") or {}).items()}
        request_body: bytes = b""
        encoded = body.get("body_b64")
        if encoded:
            import base64
            request_body = base64.b64decode(encoded)
        if quota is not None and x_darwin_id is not None:
            already_used = quota.used_today(x_darwin_id)
            if already_used >= quota.hard_bytes_per_day:
                audit.append(AccessLogEntry(
                    ts=clock(), darwin_id=x_darwin_id, method=method, url=url,
                    status=0, bytes_in=len(request_body), bytes_out=0,
                    denied_reason="bandwidth cap exceeded",
                    matched_rule=entry.host_suffix if entry else None,
                ))
                raise HTTPException(429, "bandwidth cap exceeded for today")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method, url, headers=headers, content=request_body,
            )
        import base64 as _b64
        resp_body_b64 = _b64.b64encode(resp.content).decode("ascii")
        bytes_in = len(request_body)
        bytes_out = len(resp.content)
        if quota is not None:
            allowed, denied_reason, _ = quota.check_and_charge(
                darwin_id=x_darwin_id, bytes_used=bytes_in + bytes_out,
            )
            if not allowed:
                # The response is in hand but the charge pushed us over
                # the hard cap. We still return the body the caller
                # already paid for on-chain (the egress is not the
                # billing layer), but we audit-log the breach + return
                # the rate-limit header so the client backs off.
                audit.append(AccessLogEntry(
                    ts=clock(), darwin_id=x_darwin_id, method=method, url=url,
                    status=resp.status_code, bytes_in=bytes_in,
                    bytes_out=bytes_out,
                    denied_reason=denied_reason or "bandwidth cap",
                    matched_rule=entry.host_suffix if entry else None,
                ))
                # 200/whatever from upstream — keep it but note the breach.
                return {
                    "status": resp.status_code,
                    "headers": {**dict(resp.headers),
                                "x-darwin-egress-overlimit": "true"},
                    "body_b64": resp_body_b64,
                }
        audit.append(AccessLogEntry(
            ts=clock(), darwin_id=x_darwin_id, method=method, url=url,
            status=resp.status_code, bytes_in=bytes_in,
            bytes_out=bytes_out, denied_reason=None,
            matched_rule=entry.host_suffix if entry else None,
        ))
        return {
            "status": resp.status_code,
            "headers": dict(resp.headers),
            "body_b64": resp_body_b64,
        }

    return app


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="darwin-egress-proxy")
    sub = p.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="Run the egress proxy")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", default=8080, type=int)
    serve.add_argument(
        "--allowlist", required=True, type=Path,
        help="Path to JSON allowlist (see module docstring for shape).",
    )
    serve.add_argument(
        "--bandwidth-soft-gb", default=0.0, type=float,
        help="Per-Darwin daily soft bandwidth ceiling (GB). 0 disables the "
             "quota. Doc 22 default: 10. The soft cap fires a WARN; the hard "
             "cap (--bandwidth-hard-gb) refuses with HTTP 429.",
    )
    serve.add_argument(
        "--bandwidth-hard-gb", default=0.0, type=float,
        help="Per-Darwin daily hard bandwidth ceiling (GB). 0 derives 2× the "
             "soft cap. Doc 22 default: 20 (2× burst).",
    )
    serve.add_argument("--log-level", default="info")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        import uvicorn  # type: ignore[import-not-found]
    except ImportError:
        print("uvicorn is required to serve the egress proxy.", file=sys.stderr)
        return 2
    allowlist = Allowlist.from_path(args.allowlist)
    audit = _Audit()
    # Bandwidth quota: enabled when a soft cap is configured (the serve
    # entrypoint previously never constructed one, so the per-Darwin cap was
    # unreachable in production — doc 22).
    quota: BandwidthQuota | None = None
    if args.bandwidth_soft_gb and args.bandwidth_soft_gb > 0:
        gib = 1024 * 1024 * 1024
        soft = int(args.bandwidth_soft_gb * gib)
        hard = int(
            (args.bandwidth_hard_gb or args.bandwidth_soft_gb * 2) * gib
        )
        quota = BandwidthQuota(
            soft_bytes_per_day=soft, hard_bytes_per_day=max(hard, soft),
        )
    # The egress kill-switch is always available when the proxy runs — the
    # supervisor POSTs /revoke on kill/quarantine.
    revocations = RevocationRegistry()
    app = _build_app(
        allowlist, audit, bandwidth_quota=quota, revocations=revocations,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
