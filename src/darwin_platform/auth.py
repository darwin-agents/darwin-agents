"""Platform-side auth: bearer tokens with scopes + per-Darwin ownership.

The supervisor's surface ranges over two audiences:

- **Owners** authenticate to act on their own Darwins (spawn, pause, resume,
  kill, force_reproduce, respond_escalation, ledger/events/alerts reads).
  An owner token is scoped to the Darwin ids it has been granted; calls to
  other Darwins return 403.
- **Platform operators** authenticate to do whole-fleet things — quarantine,
  install wallet policy, read the platform snapshot, read fleet alerts, and
  any owner action on any Darwin. The ``platform_operator`` scope is a
  superset of ``owner`` for permission purposes.

Auth is **optional** at the FastAPI layer: ``build_app(sup, token_store=None)``
keeps the trust-everyone behaviour for in-process tests + local dev. Real
deployments construct a ``TokenStore`` (in-memory for now; a Postgres-backed
impl would slot behind the same Protocol) and pass it in.

The store also handles the spawn-grant flow: when an owner spawns a new
Darwin, the supervisor calls ``store.grant(token, darwin_id)`` so the
caller's token gains ownership of the freshly minted id — otherwise the
owner could never act on a Darwin they just spawned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol, runtime_checkable

OWNER = "owner"
PLATFORM_OPERATOR = "platform_operator"


@dataclass
class AuthContext:
    """Identity + capabilities resolved from one bearer token."""

    token: str
    scopes: set[str] = field(default_factory=set)
    owned_darwins: set[str] = field(default_factory=set)
    subject: str | None = None  # email / user id, for audit attribution

    def has_scope(self, scope: str) -> bool:
        """``platform_operator`` implies every other scope (it's the
        superuser). Otherwise: literal scope match."""
        if PLATFORM_OPERATOR in self.scopes:
            return True
        return scope in self.scopes

    def owns(self, darwin_id: str) -> bool:
        """Platform operators can touch any Darwin. Owners only their own."""
        if PLATFORM_OPERATOR in self.scopes:
            return True
        return darwin_id in self.owned_darwins


@runtime_checkable
class TokenStore(Protocol):
    """Where bearer tokens resolve to :class:`AuthContext`. Real impls would
    talk to a Postgres + Vault-backed identity service; the in-memory one
    below is the dev/test default."""

    def lookup(self, token: str) -> AuthContext | None: ...

    def grant(self, token: str, darwin_id: str) -> None:
        """Add ``darwin_id`` to the owned-set of the token's owner. Called by
        the supervisor on a successful spawn so the caller can subsequently
        act on the new id without an out-of-band grant."""
        ...


class InMemoryTokenStore:
    """Default dev/test token store. Tokens are arbitrary strings — issue
    via :meth:`issue`, revoke via :meth:`revoke`. Owned-darwin lists
    mutate as ``grant`` is called by the supervisor on spawn."""

    def __init__(self) -> None:
        self._tokens: dict[str, AuthContext] = {}

    def issue(
        self,
        token: str,
        *,
        scopes: Iterable[str],
        owned_darwins: Iterable[str] | None = None,
        subject: str | None = None,
    ) -> AuthContext:
        """Create or replace a token with the given scopes + initial owned
        ids. Replacing is intentional — re-issuing with new scopes is how
        we tighten an existing token in tests."""
        ctx = AuthContext(
            token=token,
            scopes=set(scopes),
            owned_darwins=set(owned_darwins or ()),
            subject=subject,
        )
        self._tokens[token] = ctx
        return ctx

    def lookup(self, token: str) -> AuthContext | None:
        return self._tokens.get(token)

    def grant(self, token: str, darwin_id: str) -> None:
        ctx = self._tokens.get(token)
        if ctx is not None:
            ctx.owned_darwins.add(darwin_id)

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)


# -- a permissive "always allowed" context used when auth is disabled -----
def disabled_context() -> AuthContext:
    """Returned by the API layer when no TokenStore is configured —
    grants everything so existing in-process tests + localhost dev work
    unchanged. Real deployments MUST configure a store."""
    return AuthContext(
        token="",
        scopes={PLATFORM_OPERATOR},
        owned_darwins=set(),
        subject="anonymous",
    )
