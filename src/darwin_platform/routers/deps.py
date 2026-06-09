"""Shared FastAPI dependencies + small helpers for the router modules.

These used to be closures inside ``api.build_app``; they only read
``request.app.state`` so they lift cleanly to module scope.
"""

from __future__ import annotations

from typing import Any

from fastapi import Header, HTTPException, Request

from ..runtime.supervisor import Supervisor
from ..security.auth import AuthContext, TokenStore, disabled_context


def h_sink_read(sup: "Supervisor", darwin_id: str) -> list[dict[str, Any]]:
    """Read this Darwin's slice of the audit sink. Wraps the supervisor's
    private handle access so the WS loop has a single point to call."""
    h = sup._get(darwin_id)  # type: ignore[attr-defined]  # noqa: SLF001
    records = h.stack.audit_sink.read_all()
    return [r for r in records if r.get("darwin_id") == darwin_id]


def _stable_hash(obj: Any) -> int:
    """Cheap structural hash for change detection. ``hash(json.dumps(...))``
    is enough for "did this snapshot change" — we never persist the value."""
    import json as _json

    return hash(_json.dumps(obj, sort_keys=True, default=str))


# -- request/response models ------------------------------------------------


def _sup(request: Request) -> Supervisor:
    return request.app.state.supervisor


# -- auth dependency --------------------------------------------------
def get_auth(
    request: Request,
    authorization: str | None = Header(default=None),
) -> AuthContext:
    store: TokenStore | None = request.app.state.token_store
    if store is None:
        # No store configured → permissive mode (the existing in-process
        # tests rely on this). Documented in build_app's docstring.
        return disabled_context()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            401,
            "missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[len("Bearer ") :].strip()
    ctx = store.lookup(token)
    if ctx is None:
        raise HTTPException(401, "invalid token", headers={"WWW-Authenticate": "Bearer"})
    return ctx


def _require_scope(ctx: AuthContext, scope: str) -> None:
    if not ctx.has_scope(scope):
        raise HTTPException(403, f"requires scope: {scope}")


def _require_owns(ctx: AuthContext, darwin_id: str) -> None:
    if not ctx.owns(darwin_id):
        raise HTTPException(403, f"forbidden: not your darwin: {darwin_id}")


def _actor(ctx: AuthContext) -> str:
    """Operator identity for the audit log: prefer the human-readable
    ``subject`` (email / user id), fall back to the token, fall back to
    ``anonymous`` (auth-disabled mode)."""
    return ctx.subject or ctx.token or "anonymous"
