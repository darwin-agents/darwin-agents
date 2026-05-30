#!/usr/bin/env python3
"""Darwin platform smoke test — WebSocket live-stream variant.

Same interaction story as ``smoke_test.sh`` (spawn parent A -> watch it
think -> reproduce into child B -> kill both), but instead of polling the
REST event log it attaches to the live multiplexed stream at
``/darwins/{id}/stream`` and prints events as they happen.

REST calls use stdlib urllib (no dependency). The WebSocket uses the
``websockets`` package:  pip install websockets

Usage:
    ./smoke_stream.py                 # full flow against http://localhost:8000
    ./smoke_stream.py darwin-abc123   # just live-tail an existing Darwin
    BASE=http://host:8000 WATCH_SECS=60 ./smoke_stream.py

Auth: permissive when the supervisor runs without --auth-tokens (the compose
default). Set TOKEN=... to send a bearer token (?token= handshake).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("BASE", "http://localhost:8000")
WS_BASE = BASE.replace("https://", "wss://").replace("http://", "ws://")
TOKEN = os.environ.get("TOKEN")
OPENING = float(os.environ.get("OPENING", "100"))
WATCH_SECS = float(os.environ.get("WATCH_SECS", "45"))

# Highlight the kinds that prove real cognition / cross-Darwin interaction.
# Real LLM work is embedded inside the perceive/plan/reflect drive-loop steps
# (not emitted as separate llm.* events), so match those kinds directly.
HOT = ("perceive", "plan", "reflect", "neuron", "outcome", "payment",
       "reproduc", "lineage", "birth", "harvest", "escal", "llm")


def _req(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"content-type": "application/json"},
    )
    if TOKEN:
        req.add_header("authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (trusted local URL)
        raw = r.read().decode()
    return json.loads(raw) if raw else {}


def _hot(kind: str) -> bool:
    return any(kind.startswith(p) or p in kind for p in HOT)


async def stream(darwin_id: str, seconds: float, *, label: str = "") -> None:
    """Attach to the live WS and print events until `seconds` elapse."""
    try:
        import websockets
    except ImportError:
        sys.exit("need the 'websockets' package: pip install websockets")

    url = f"{WS_BASE}/darwins/{darwin_id}/stream"
    if TOKEN:
        url += f"?token={TOKEN}"
    tag = f"[{label or darwin_id}]"
    print(f"{tag} connecting {url}")
    async with websockets.connect(url, max_size=None) as ws:
        # Limit fan-out to the timeline + status channels for a readable tail.
        await ws.send(json.dumps({"type": "subscribe",
                                  "channels": ["timeline", "status"]}))
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            except Exception as exc:  # connection closed etc.
                print(f"{tag} stream closed: {exc}")
                break
            obj = json.loads(msg)
            t = obj.get("type")
            if t in ("timeline.event",):
                ev = obj.get("payload", {})
                kind = str(ev.get("kind", ""))
                if not _hot(kind):
                    continue
                p = ev.get("payload", {}) or {}
                extra = p.get("tier") or p.get("neuron_id") or p.get("amount_usdc") or ""
                print(f"{tag} seq={ev.get('seq')} {kind}  {extra}")
            elif t in ("snapshot.status", "status"):
                s = obj.get("payload", {})
                print(f"{tag} status: tick={s.get('tick')} "
                      f"balance=${s.get('balance_usdc')} alive={s.get('alive')}")
            elif t == "escalation.open":
                print(f"{tag} ESCALATION {obj.get('payload', {}).get('ticket_id')}")
            elif t == "error":
                print(f"{tag} error: {obj.get('code')} {obj.get('detail')}")


async def main() -> None:
    # Mode A: an id was passed -> just live-tail it.
    if len(sys.argv) > 1:
        await stream(sys.argv[1], WATCH_SECS)
        return

    # Mode B: full interaction flow.
    print(f"== health: {_req('GET', '/healthz')} ==")

    a = _req("POST", "/darwins",
             {"opening_usdc": OPENING, "escalation_auto": "approve"})
    A = a["darwin_id"]
    print(f"== spawned parent A = {A} (opening ${OPENING}) ==")

    print(f"== live-streaming A for {WATCH_SECS:.0f}s (watch it think) ==")
    await stream(A, WATCH_SECS, label="A")

    print("== forcing A to reproduce -> child B ==")
    _req("POST", f"/darwins/{A}/reproduce/force")

    B = ""
    for _ in range(12):
        await asyncio.sleep(5)
        edges = _req("GET", f"/darwins/{A}/lineage").get("edges", [])
        kids = [e["child_id"] for e in edges if e.get("parent_id") == A]
        if kids:
            B = kids[-1]
            break
    if not B:
        print("no child yet — raise WATCH_SECS / OPENING and retry")
    else:
        print(f"== child B = {B}; live-streaming B's first moments ==")
        await stream(B, 20, label="B")
        print("-- lineage tree --")
        print(json.dumps(_req("GET", f"/darwins/{A}/lineage/tree"), indent=2))
        print(f"-- A wallet: {_req('GET', f'/darwins/{A}')['balance_usdc']}  "
              f"B wallet: {_req('GET', f'/darwins/{B}')['balance_usdc']}")

    print("== cleanup: killing both ==")
    _req("POST", f"/darwins/{A}/kill", {"reason": "smoke stream done"})
    if B:
        _req("POST", f"/darwins/{B}/kill", {"reason": "smoke stream done"})
    print("done.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted — kill leftover darwins via the API or `docker compose down`")
