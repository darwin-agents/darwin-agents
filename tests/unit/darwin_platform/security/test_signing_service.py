"""Tests for the Phase 1 HSM-backed signing service (docs/engineering/21).

Covers:
- LocalEd25519Signer: round-trips through reproduction's verifier.
- KMSSigningService: wraps a stub backend and returns its sig + PEM.
- Empty PEM / empty sig from backend ⇒ SigningServiceError.
- Backend exceptions become SigningServiceError (with cause chained).
- build_signer factory selects KMS backend → Hvac → Local in order.
- HvacTransitBackend parses the ``vault:vN:`` signature prefix correctly
  and extracts the public_key PEM from the latest key version.
"""

from __future__ import annotations

import base64
import types

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from darwin_dna.signature import load_platform_public_key, verify
from darwin_platform.security.signing_service import (
    HvacTransitBackend,
    KMSBackend,
    KMSSigningService,
    LocalEd25519Signer,
    SigningServiceError,
    build_signer,
)


# ---------------------------------------------------------------------------
# LocalEd25519Signer (dev mode)
# ---------------------------------------------------------------------------

def test_local_signer_round_trips_through_verifier():
    signer = LocalEd25519Signer()
    msg = b"darwin layer payload"
    sig = signer.sign(msg)
    pub = load_platform_public_key(signer.public_key_pem())
    verify(pub, msg, sig)  # raises on failure


def test_local_signer_restore_from_private_bytes_preserves_pubkey():
    a = LocalEd25519Signer()
    raw = a.private_bytes()
    b = LocalEd25519Signer(raw_private=raw)
    assert a.public_key_pem() == b.public_key_pem()
    # And signatures from b verify against a's public key.
    sig = b.sign(b"x")
    pub = load_platform_public_key(a.public_key_pem())
    verify(pub, b"x", sig)


# ---------------------------------------------------------------------------
# KMSSigningService with a stub backend
# ---------------------------------------------------------------------------

class _StubBackend:
    def __init__(self, *, pem=b"-----BEGIN PUBLIC KEY-----\nQQ==\n-----END PUBLIC KEY-----\n",
                 sig=b"\x01\x02\x03", raises_on_sign: Exception | None = None,
                 raises_on_pem: Exception | None = None):
        self._pem = pem
        self._sig = sig
        self._raises_on_sign = raises_on_sign
        self._raises_on_pem = raises_on_pem
        self.sign_calls: list[bytes] = []

    def public_key_pem(self) -> bytes:
        if self._raises_on_pem is not None:
            raise self._raises_on_pem
        return self._pem

    def sign(self, message: bytes) -> bytes:
        self.sign_calls.append(bytes(message))
        if self._raises_on_sign is not None:
            raise self._raises_on_sign
        return self._sig


def test_kms_service_caches_pem_and_delegates_sign():
    backend = _StubBackend()
    svc = KMSSigningService(backend=backend)
    assert svc.public_key_pem() == backend._pem
    sig = svc.sign(b"hello")
    assert sig == b"\x01\x02\x03"
    assert backend.sign_calls == [b"hello"]


def test_kms_service_rejects_empty_pem():
    backend = _StubBackend(pem=b"")
    with pytest.raises(SigningServiceError):
        KMSSigningService(backend=backend)


def test_kms_service_rejects_empty_signature():
    backend = _StubBackend(sig=b"")
    svc = KMSSigningService(backend=backend)
    with pytest.raises(SigningServiceError):
        svc.sign(b"x")


def test_kms_service_wraps_backend_sign_errors():
    backend = _StubBackend(raises_on_sign=ConnectionError("hsm down"))
    svc = KMSSigningService(backend=backend)
    with pytest.raises(SigningServiceError) as ei:
        svc.sign(b"x")
    assert "hsm down" in str(ei.value)


def test_kms_service_wraps_pem_fetch_errors():
    backend = _StubBackend(raises_on_pem=ConnectionError("kms unauthorized"))
    with pytest.raises(SigningServiceError) as ei:
        KMSSigningService(backend=backend)
    assert "kms unauthorized" in str(ei.value)


# ---------------------------------------------------------------------------
# HvacTransitBackend with a stub hvac client
# ---------------------------------------------------------------------------

def _real_ed25519_pem() -> bytes:
    sk = Ed25519PrivateKey.generate()
    return sk.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


class _StubTransitClient:
    """Mimics hvac.Client.secrets.transit shape."""

    def __init__(self, *, pem: bytes, sig_b64: str):
        self.secrets = types.SimpleNamespace(
            transit=self,  # so client.secrets.transit.read_key works
        )
        self._pem = pem
        self._sig_b64 = sig_b64
        self.sign_calls: list[dict] = []

    def read_key(self, *, name, mount_point):
        return {
            "data": {
                "keys": {
                    "1": {"public_key": self._pem.decode()},
                    "2": {"public_key": self._pem.decode()},
                }
            }
        }

    def sign_data(self, *, name, hash_input, mount_point):
        self.sign_calls.append({
            "name": name, "hash_input": hash_input,
            "mount_point": mount_point,
        })
        return {"data": {"signature": f"vault:v2:{self._sig_b64}"}}


def test_hvac_transit_backend_returns_latest_pem_and_decodes_sig():
    pem = _real_ed25519_pem()
    sig_b64 = base64.b64encode(b"\xaa" * 64).decode()
    client = _StubTransitClient(pem=pem, sig_b64=sig_b64)
    backend = HvacTransitBackend(
        client=client, mount="transit", key_name="darwin-platform",
    )
    assert backend.public_key_pem() == pem
    sig = backend.sign(b"hi")
    assert sig == b"\xaa" * 64
    assert client.sign_calls[0]["name"] == "darwin-platform"


def test_hvac_transit_backend_rejects_empty_key():
    backend_factory = lambda: HvacTransitBackend(
        client=None, mount="transit", key_name="x",
    )
    with pytest.raises(SigningServiceError):
        backend_factory()


# ---------------------------------------------------------------------------
# build_signer factory
# ---------------------------------------------------------------------------

def test_build_signer_defaults_to_local():
    s = build_signer()
    assert isinstance(s, LocalEd25519Signer)


def test_build_signer_prefers_kms_backend():
    backend = _StubBackend()
    s = build_signer(kms_backend=backend)
    assert isinstance(s, KMSSigningService)


def test_build_signer_uses_hvac_when_only_client_given():
    pem = _real_ed25519_pem()
    sig_b64 = base64.b64encode(b"\xbb" * 64).decode()
    client = _StubTransitClient(pem=pem, sig_b64=sig_b64)
    s = build_signer(
        hvac_client=client, transit_key_name="darwin-platform",
    )
    assert isinstance(s, KMSSigningService)
    sig = s.sign(b"x")
    assert sig == b"\xbb" * 64
