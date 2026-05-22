"""Clerk auth — RS256 JWT verification + the TokenStore (docs/engineering/08).

Tests sign real JWTs with a generated RSA keypair and verify them through
the real PyJWT path — no mocked crypto. The JWKS fetch is bypassed by
injecting ``signing_key_resolver``.
"""

from __future__ import annotations

import time

import pytest

jwt = pytest.importorskip("jwt")

from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from darwin_platform.security.auth import (  # noqa: E402
    OWNER,
    PLATFORM_OPERATOR,
    TokenStore,
)
from darwin_platform.security.clerk import (  # noqa: E402
    ClerkAuthError,
    ClerkTokenStore,
    ClerkVerifier,
)

# Module-scoped keypairs — RSA generation is the slow bit; do it once.
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUB = _KEY.public_key()
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_ISSUER = "https://darwin.clerk.accounts.dev"


def _mint(
    *, key=_KEY, sub="user_abc", scopes=None, issuer=_ISSUER,
    audience=None, exp_delta=3600, **extra,
) -> str:
    now = int(time.time())
    claims: dict = {"sub": sub, "iat": now, "exp": now + exp_delta}
    if issuer is not None:
        claims["iss"] = issuer
    if audience is not None:
        claims["aud"] = audience
    if scopes is not None:
        claims["scopes"] = scopes
    claims.update(extra)
    return jwt.encode(claims, key, algorithm="RS256")


def _verifier(**kw) -> ClerkVerifier:
    kw.setdefault("issuer", _ISSUER)
    kw.setdefault("signing_key_resolver", lambda _t: _PUB)
    return ClerkVerifier(**kw)


# -- ClerkVerifier ---------------------------------------------------------

def test_verifier_accepts_a_valid_token():
    claims = _verifier().verify(_mint(sub="user_42"))
    assert claims["sub"] == "user_42"


def test_verifier_rejects_an_expired_token():
    with pytest.raises(ClerkAuthError):
        _verifier().verify(_mint(exp_delta=-3600))


def test_verifier_rejects_a_wrong_signature():
    # Signed with a different key than the resolver hands back.
    with pytest.raises(ClerkAuthError):
        _verifier().verify(_mint(key=_OTHER_KEY))


def test_verifier_rejects_a_wrong_issuer():
    with pytest.raises(ClerkAuthError):
        _verifier().verify(_mint(issuer="https://evil.example"))


def test_verifier_rejects_an_empty_token():
    with pytest.raises(ClerkAuthError):
        _verifier().verify("")


def test_verifier_audience_when_configured():
    v = _verifier(audience="darwin-api")
    # Right audience verifies.
    assert v.verify(_mint(audience="darwin-api"))["sub"] == "user_abc"
    # Wrong audience is rejected.
    with pytest.raises(ClerkAuthError):
        v.verify(_mint(audience="some-other-api"))
    # Missing audience when one is required is rejected.
    with pytest.raises(ClerkAuthError):
        v.verify(_mint())


def test_verifier_ctor_needs_a_key_source():
    with pytest.raises(ValueError):
        ClerkVerifier()


def test_verifier_derives_jwks_url_from_issuer():
    v = ClerkVerifier(issuer="https://x.clerk.accounts.dev")
    assert v._jwks_url == "https://x.clerk.accounts.dev/.well-known/jwks.json"


# -- ClerkTokenStore -------------------------------------------------------

def test_token_store_satisfies_the_token_store_protocol():
    store = ClerkTokenStore(verifier=_verifier())
    assert isinstance(store, TokenStore)  # drop-in for InMemoryTokenStore


def test_lookup_resolves_an_auth_context():
    store = ClerkTokenStore(verifier=_verifier())
    ctx = store.lookup(_mint(sub="user_99", scopes=["owner"]))
    assert ctx is not None
    assert ctx.subject == "user_99"
    assert ctx.has_scope(OWNER)


def test_lookup_returns_none_for_a_bad_token():
    store = ClerkTokenStore(verifier=_verifier())
    assert store.lookup(_mint(exp_delta=-3600)) is None
    assert store.lookup("not-a-jwt") is None


def test_lookup_returns_none_when_subject_missing():
    # A token with no `sub` cannot be attributed to a user.
    store = ClerkTokenStore(verifier=_verifier())
    assert store.lookup(_mint(sub=None)) is None


def test_scopes_default_to_owner_when_claim_absent():
    store = ClerkTokenStore(verifier=_verifier())
    ctx = store.lookup(_mint())  # no scopes claim
    assert ctx is not None
    assert ctx.scopes == {OWNER}


def test_scopes_parse_from_a_list_claim():
    store = ClerkTokenStore(verifier=_verifier())
    ctx = store.lookup(_mint(scopes=["owner", "platform_operator"]))
    assert ctx is not None
    assert ctx.has_scope(PLATFORM_OPERATOR)


def test_scopes_parse_from_a_space_or_comma_string():
    store = ClerkTokenStore(verifier=_verifier())
    ctx = store.lookup(_mint(scopes="owner, platform_operator"))
    assert ctx is not None
    assert ctx.scopes == {OWNER, PLATFORM_OPERATOR}


def test_custom_scopes_claim_name():
    store = ClerkTokenStore(
        verifier=_verifier(), scopes_claim="darwin_scopes",
    )
    ctx = store.lookup(_mint(darwin_scopes=["platform_operator"]))
    assert ctx is not None
    assert ctx.has_scope(PLATFORM_OPERATOR)


def test_grant_records_ownership_keyed_by_clerk_user():
    store = ClerkTokenStore(verifier=_verifier())
    token = _mint(sub="user_owns")
    assert store.lookup(token).owned_darwins == set()

    store.grant(token, "darwin-1")
    # A fresh (rotated) token for the SAME user still sees the ownership.
    rotated = _mint(sub="user_owns")
    ctx = store.lookup(rotated)
    assert ctx.owned_darwins == {"darwin-1"}
    assert ctx.owns("darwin-1")


def test_grant_is_a_noop_for_an_invalid_token():
    store = ClerkTokenStore(verifier=_verifier())
    store.grant("garbage", "darwin-1")  # no raise
    assert store.lookup(_mint(sub="user_x")).owned_darwins == set()


def test_cli_parses_clerk_flags():
    from darwin_platform.__main__ import build_parser
    args = build_parser().parse_args([
        "serve",
        "--clerk-issuer", "https://x.clerk.accounts.dev",
        "--clerk-audience", "darwin-api",
    ])
    assert args.clerk_issuer == "https://x.clerk.accounts.dev"
    assert args.clerk_audience == "darwin-api"
    assert args.clerk_jwks_url is None
