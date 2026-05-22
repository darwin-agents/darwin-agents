"""Clerk authentication for the control-plane API (docs/engineering/08, 22).

Clerk is the identity provider: it issues short-lived RS256 JWTs to
signed-in owners and operators. The platform *verifies* those JWTs against
Clerk's public JWKS — it never sees a password and needs no Clerk secret
for the verify path (the JWKS is public).

Two pieces:

- :class:`ClerkVerifier` — verifies a Clerk JWT (RS256 signature against
  the JWKS, ``exp`` / ``nbf``, optional ``iss`` / ``aud``) and returns its
  claims, or raises :class:`ClerkAuthError`.
- :class:`ClerkTokenStore` — implements the platform's ``TokenStore``
  Protocol on top of the verifier. AUTHENTICATION (who you are) comes from
  Clerk; AUTHORIZATION (which Darwins you own) is platform state, held per
  Clerk user id (the JWT ``sub``) so it survives token rotation.

Drop-in: ``build_app(sup, token_store=ClerkTokenStore(...))`` and every
existing scope / ownership check works unchanged.
"""

from __future__ import annotations

from typing import Any, Callable

from .auth import OWNER, AuthContext

DEFAULT_SCOPES_CLAIM = "scopes"


class ClerkAuthError(Exception):
    """A Clerk JWT failed verification — bad signature, expired, wrong
    issuer / audience, or malformed. :class:`ClerkTokenStore` turns this
    into a ``lookup`` miss, which the API renders as HTTP 401."""


def _import_jwt() -> Any:
    try:
        import jwt
    except ImportError as exc:  # pragma: no cover - operator hint
        raise ImportError(
            "Clerk auth requires PyJWT: pip install 'darwin-dna[platform]'"
        ) from exc
    return jwt


class ClerkVerifier:
    """Verifies a Clerk-issued RS256 JWT against Clerk's JWKS.

    Give it ``jwks_url`` directly, or an ``issuer`` (the JWKS is then
    ``<issuer>/.well-known/jwks.json``). Tests inject ``signing_key_
    resolver`` — a ``callable(token) -> key`` — to avoid a network fetch.
    """

    def __init__(
        self,
        *,
        jwks_url: str | None = None,
        issuer: str | None = None,
        audience: str | None = None,
        leeway_seconds: float = 10.0,
        signing_key_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        if not (jwks_url or issuer or signing_key_resolver):
            raise ValueError(
                "ClerkVerifier needs jwks_url, issuer, or a "
                "signing_key_resolver"
            )
        self._issuer = issuer
        self._audience = audience
        self._leeway = float(leeway_seconds)
        self._jwks_url = jwks_url or (
            f"{issuer.rstrip('/')}/.well-known/jwks.json" if issuer else None
        )
        self._resolver = signing_key_resolver
        self._jwk_client: Any = None  # lazily-built PyJWKClient

    def _resolve_key(self, token: str) -> Any:
        """Return the RSA public key that signed ``token`` — from the
        injected resolver, or by matching the JWT's ``kid`` against the
        JWKS."""
        if self._resolver is not None:
            return self._resolver(token)
        if not self._jwks_url:
            raise ClerkAuthError("no JWKS url configured")
        jwt = _import_jwt()
        if self._jwk_client is None:
            self._jwk_client = jwt.PyJWKClient(self._jwks_url)
        try:
            return self._jwk_client.get_signing_key_from_jwt(token).key
        except Exception as exc:  # noqa: BLE001 - any JWKS failure → auth fail
            raise ClerkAuthError(
                f"could not resolve signing key: {exc}"
            ) from exc

    def verify(self, token: str) -> dict[str, Any]:
        """Verify ``token`` and return its claims. Raise
        :class:`ClerkAuthError` on any failure."""
        if not token:
            raise ClerkAuthError("empty token")
        jwt = _import_jwt()
        key = self._resolve_key(token)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audience,
                leeway=self._leeway,
                options={"verify_aud": self._audience is not None},
            )
        except Exception as exc:  # noqa: BLE001 - PyJWT raises many subtypes
            raise ClerkAuthError(f"JWT verification failed: {exc}") from exc
        return dict(claims)


class ClerkTokenStore:
    """``TokenStore`` backed by Clerk JWT verification.

    ``lookup`` verifies the bearer JWT and resolves an :class:`AuthContext`;
    ``grant`` records Darwin ownership against the Clerk user id. The
    ownership map is platform state — in-memory here; a Postgres-backed
    map slots in behind the same shape for a multi-replica deployment.
    """

    def __init__(
        self,
        *,
        verifier: ClerkVerifier,
        scopes_claim: str = DEFAULT_SCOPES_CLAIM,
        default_scopes: tuple[str, ...] = (OWNER,),
        ownership: dict[str, set[str]] | None = None,
    ) -> None:
        self._verifier = verifier
        self._scopes_claim = scopes_claim
        self._default_scopes = frozenset(default_scopes)
        self._ownership: dict[str, set[str]] = {
            str(k): set(v) for k, v in (ownership or {}).items()
        }

    def lookup(self, token: str) -> AuthContext | None:
        try:
            claims = self._verifier.verify(token)
        except ClerkAuthError:
            return None
        subject = str(claims.get("sub") or "").strip()
        if not subject:
            return None
        return AuthContext(
            token=token,
            scopes=self._scopes_from(claims),
            owned_darwins=set(self._ownership.get(subject, set())),
            subject=subject,
        )

    def grant(self, token: str, darwin_id: str) -> None:
        try:
            claims = self._verifier.verify(token)
        except ClerkAuthError:
            return
        subject = str(claims.get("sub") or "").strip()
        if subject:
            self._ownership.setdefault(subject, set()).add(darwin_id)

    def _scopes_from(self, claims: dict[str, Any]) -> set[str]:
        """Parse scopes from the configured claim. Accepts a list or a
        space/comma-separated string; an absent claim falls back to the
        default (a verified Clerk user is at least an ``owner`` —
        ``platform_operator`` must be granted explicitly)."""
        raw = claims.get(self._scopes_claim)
        if isinstance(raw, str):
            parts: list[str] = raw.replace(",", " ").split()
        elif isinstance(raw, (list, tuple, set)):
            parts = [str(x) for x in raw]
        else:
            parts = []
        scopes = {p.strip() for p in parts if p.strip()}
        return scopes or set(self._default_scopes)
