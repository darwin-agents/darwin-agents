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
import asyncio
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


def _build_app(allowlist: Allowlist, audit: _Audit, *, clock=time.time):
    try:
        from fastapi import Body, FastAPI, Header, HTTPException
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "egress_proxy requires the [platform] extra (FastAPI + httpx)"
        ) from exc

    app = FastAPI(title="darwin-egress-proxy")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/access-log")
    async def access_log(limit: int = 200) -> list[dict[str, Any]]:
        return audit.snapshot(limit=min(max(limit, 1), 2000))

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
        headers = {str(k): str(v) for k, v in (body.get("headers") or {}).items()}
        request_body: bytes = b""
        encoded = body.get("body_b64")
        if encoded:
            import base64
            request_body = base64.b64decode(encoded)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method, url, headers=headers, content=request_body,
            )
        import base64 as _b64
        resp_body_b64 = _b64.b64encode(resp.content).decode("ascii")
        audit.append(AccessLogEntry(
            ts=clock(), darwin_id=x_darwin_id, method=method, url=url,
            status=resp.status_code, bytes_in=len(request_body),
            bytes_out=len(resp.content), denied_reason=None,
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
    app = _build_app(allowlist, audit)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
