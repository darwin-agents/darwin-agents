"""Stream routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Stream' tag instead of the catch-all default.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from fastapi import (
    APIRouter,
    WebSocket,
    WebSocketDisconnect,
)

from ..runtime.supervisor import Supervisor, UnknownDarwin
from ..security.auth import (
    OWNER,
    TokenStore,
    disabled_context,
)

from .deps import (
    _stable_hash,
    h_sink_read,
)

router = APIRouter(tags=["Stream"])


# -- WebSocket stream (frontend 07) ------------------------------------
@router.websocket("/darwins/{darwin_id}/stream")
async def darwin_stream(websocket: WebSocket, darwin_id: str) -> None:
    """Multiplexed live stream for the dashboard.

    Messages from server:
      - {"type": "snapshot.anatomy", "payload": <anatomy>}
      - {"type": "snapshot.status",  "payload": <status>}
      - {"type": "timeline.event",   "payload": <event>}
      - {"type": "escalation.open",  "payload": <ticket>}
      - {"type": "anatomy.patch",    "payload": {...}}   (only if changed)
      - {"type": "status",           "payload": <status>} (only if changed)
      - {"type": "heartbeat",        "ts": <unix>}
      - {"type": "error",            "code": ..., "detail": ...}

    Client may send an initial {"type": "subscribe", "channels": [...]}
    to limit fan-out; "since" optionally resyncs from a seq cursor.

    Auth: bearer token via "Authorization" header (passed by the
    Next.js WS client wrapper) or "?token=" query as a Cloudflare-tunnel
    fallback. Auth state is validated once at connect.
    """
    import asyncio
    import json

    # --- Auth handshake (header preferred, query fallback) -----------
    token: str | None = None
    auth_header = websocket.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[len("Bearer ") :].strip()
    if token is None:
        token = websocket.query_params.get("token")
    store: TokenStore | None = websocket.app.state.token_store
    if store is None:
        ctx = disabled_context()
    else:
        if not token:
            await websocket.close(code=4401, reason="missing token")
            return
        ctx = store.lookup(token)
        if ctx is None:
            await websocket.close(code=4401, reason="invalid token")
            return
    if not ctx.has_scope(OWNER) or not ctx.owns(darwin_id):
        await websocket.close(code=4403, reason="forbidden")
        return

    sup: Supervisor = websocket.app.state.supervisor
    if not sup.known_darwin(darwin_id):
        await websocket.close(code=4404, reason="unknown darwin")
        return
    # Pod-hosted Darwins are served over control RPCs, not in-memory reads —
    # poll less aggressively so the stream doesn't hammer the pod.
    is_k8s = sup._is_k8s(darwin_id)  # noqa: SLF001
    poll_scale = 5 if is_k8s else 1

    await websocket.accept()

    # Channel subscription — default to all. Updated by client message.
    channels = {"timeline", "anatomy", "status", "escalations"}
    since_seq: int = -1

    async def _send(obj: dict[str, Any]) -> bool:
        try:
            await websocket.send_text(json.dumps(obj, default=str))
            return True
        except (RuntimeError, WebSocketDisconnect):
            return False

    # Initial snapshot fan-out so the client doesn't need to follow up
    # with separate REST calls before WS data starts flowing.
    try:
        if "anatomy" in channels:
            anatomy = await sup.anatomy(darwin_id)
            if not await _send({"type": "snapshot.anatomy", "payload": anatomy}):
                return
        if "status" in channels:
            status_obj = asdict(await sup.status(darwin_id))
            if not await _send({"type": "snapshot.status", "payload": status_obj}):
                return
    except Exception as exc:  # pragma: no cover - best-effort initial snapshot
        await _send({"type": "error", "code": "snapshot_failed", "detail": str(exc)})

    # Side task that drains client messages on its own schedule —
    # robust against TestClient's send timing and supports late
    # `subscribe` resyncs (e.g. when the dashboard pauses/resumes).
    state = {"channels": channels, "since_seq": since_seq, "applied": False}

    async def _drain_client() -> None:
        while True:
            try:
                msg = await websocket.receive_text()
            except WebSocketDisconnect:
                return
            try:
                parsed = json.loads(msg)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(parsed, dict):
                continue
            if parsed.get("type") == "subscribe":
                ch = parsed.get("channels")
                if isinstance(ch, list):
                    state["channels"] = {str(c) for c in ch}
                s = parsed.get("since")
                if isinstance(s, int):
                    state["since_seq"] = int(s)
                    state["applied"] = True

    drain_task = asyncio.create_task(_drain_client())
    # Give a brief window for any immediate client subscribe to land
    # before the polling loop reads `state` for the first time.
    await asyncio.sleep(0.05)
    channels = state["channels"]
    since_seq = state["since_seq"]

    # --- Polling loop ------------------------------------------------
    # Poll deltas: events on every tick, anatomy/status every other.
    # Cheap: read_all() is in-memory; the dashboard load is bounded by
    # how many tabs an operator opens (typically <5).
    last_status: dict[str, Any] | None = None
    last_anatomy_hash: int | None = None
    seen_ticket_ids: set[str] = set()
    tick = 0
    try:
        while True:
            tick += 1
            # Refresh in case a late `subscribe` arrived.
            channels = state["channels"]
            if state.get("applied"):
                since_seq = max(since_seq, state["since_seq"])
                state["applied"] = False

            # 1. New events since last cursor.
            if "timeline" in channels and tick % poll_scale == 0:
                try:
                    if is_k8s:
                        # Pod-hosted: the audit log lives in the pod; the
                        # supervisor's events() proxies + filters it.
                        resp = await sup.events(
                            darwin_id, since=max(since_seq, -1), limit=1000,
                        )
                        recs = list(resp.get("events") or [])
                        if since_seq < 0 and recs:
                            # Don't replay history — start from the live head.
                            since_seq = int(recs[-1].get("seq", -1))
                            recs = []
                        new_events = recs  # since-mode is already filtered
                    else:
                        recs = h_sink_read(sup, darwin_id)
                        if since_seq < 0 and recs:
                            # Don't replay history — start from the live head.
                            since_seq = int(recs[-1].get("seq", -1))
                        new_events = [
                            r for r in recs if int(r.get("seq", -1)) > since_seq
                        ]
                except UnknownDarwin:
                    await _send({"type": "error", "code": "gone", "detail": "darwin removed"})
                    await websocket.close(code=4410)
                    return
                except Exception:
                    # Transient pod RPC failure — keep the socket, retry on
                    # the next poll.
                    new_events = []
                for ev in new_events:
                    if not await _send({"type": "timeline.event", "payload": ev}):
                        return
                    since_seq = max(since_seq, int(ev.get("seq", since_seq)))

            # 2. Status diff (every 2 ticks ~ 400ms; scaled for k8s).
            if "status" in channels and tick % (2 * poll_scale) == 0:
                try:
                    status_obj = asdict(await sup.status(darwin_id))
                except Exception:
                    status_obj = None
                if status_obj is not None and status_obj != last_status:
                    last_status = status_obj
                    if not await _send({"type": "status", "payload": status_obj}):
                        return

            # 3. Anatomy diff (every 5 ticks ~ 1s; scaled for k8s).
            if "anatomy" in channels and tick % (5 * poll_scale) == 0:
                try:
                    anatomy = await sup.anatomy(darwin_id)
                except Exception:
                    anatomy = None
                if anatomy is not None:
                    h = _stable_hash(anatomy)
                    if h != last_anatomy_hash:
                        last_anatomy_hash = h
                        if not await _send({"type": "anatomy.patch", "payload": anatomy}):
                            return

            # 4. New escalations.
            if "escalations" in channels and tick % (3 * poll_scale) == 0:
                try:
                    tickets = await sup.open_escalations(darwin_id)
                except Exception:
                    tickets = []
                for tk in tickets:
                    tid = str(tk.get("ticket_id"))
                    if tid and tid not in seen_ticket_ids:
                        seen_ticket_ids.add(tid)
                        if not await _send({"type": "escalation.open", "payload": tk}):
                            return

            # 5. Heartbeat every ~5s.
            if tick % 25 == 0:
                if not await _send({"type": "heartbeat", "ts": time.time()}):
                    return

            await asyncio.sleep(0.2)
    except WebSocketDisconnect:
        return
