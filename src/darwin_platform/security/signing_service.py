"""HSM-/KMS-backed PlatformSigner (docs/engineering/21 §"DNA signing service").

Phase 0 uses an in-memory Ed25519 key
(:class:`darwin_fakes.fake_platform.SigningService`) — fine for tests
and the 90-day sim. Phase 1 moves to a real HSM-backed key whose
private bytes never leave the secure module: the platform asks the
service to sign; the service replies with the signature. The genome
only ever holds the public-key PEM.

The trust root (:func:`darwin_dna.signature.load_platform_public_key`)
is strict Ed25519. Phase 1 supports two HSM backends out of the box:

* :class:`HvacTransitBackend` — HashiCorp Vault Transit engine; Ed25519
  is supported natively, so the manifest verifier works unchanged.
* :class:`KMSBackend` — a generic Protocol the platform can implement
  against any HSM (Google Cloud KMS, Azure Key Vault, AWS CloudHSM via
  PKCS#11, or a hardware appliance) as long as it can sign with the
  Ed25519 scheme. AWS KMS itself does not currently support Ed25519
  natively — for AWS-only deployments, Vault Transit on top of KMS is
  the recommended path.

Both are wrapped by :class:`KMSSigningService`, which implements the
:class:`PlatformSigner` Protocol expected by
:mod:`darwin_dna.evolution.reproduction`. A small
:class:`LocalEd25519Signer` re-export covers dev mode + HA persistence
so the supervisor's signer-construction site has one factory:
:func:`build_signer`.
"""

from __future__ import annotations

import base64
from typing import Any, Protocol, runtime_checkable

from darwin_fakes.fake_platform import SigningService as _LocalSigner


@runtime_checkable
class PlatformSigner(Protocol):
    """The shape :mod:`darwin_dna.evolution.reproduction` and
    :func:`mint_genesis` consume. Implementations may hold the key
    locally (dev) or delegate to an HSM (production)."""

    def sign(self, message: bytes) -> bytes: ...

    def public_key_pem(self) -> bytes: ...


class SigningServiceError(Exception):
    """Raised on signer construction or sign-call failure. The supervisor
    surfaces this as a 5xx (signing is platform infrastructure)."""


# -- local-key signer (dev + HA) -------------------------------------------
class LocalEd25519Signer:
    """In-process Ed25519 signer, identical wire-format to the HSM-backed
    one. Production deployments should NOT use this — the key bytes live
    in memory. The supervisor uses it as a fallback when no
    ``--signing-kms-key-id`` flag is set; HA persistence reloads the
    private bytes via :meth:`private_bytes` so signatures from before a
    restart still verify."""

    def __init__(self, *, raw_private: bytes | None = None) -> None:
        if raw_private is None:
            self._inner = _LocalSigner()
        else:
            self._inner = _LocalSigner.from_private_bytes(raw_private)

    def sign(self, message: bytes) -> bytes:
        return self._inner.sign(message)

    def public_key_pem(self) -> bytes:
        return self._inner.public_key_pem()

    def private_bytes(self) -> bytes:
        """Used by HA persistence ONLY (never written to audit/logs). The
        bytes are the raw 32-byte Ed25519 private key; loading them into a
        new :class:`LocalEd25519Signer` preserves the same public key."""
        return self._inner.private_bytes()


# -- HSM-backed signers ----------------------------------------------------
@runtime_checkable
class KMSBackend(Protocol):
    """The minimum surface :class:`KMSSigningService` needs from an HSM
    backend. Implementations may wrap boto3, hvac, the Google KMS SDK,
    or any PKCS#11 token.

    ``sign`` takes the raw message bytes (the caller — the reproduction
    flow — has already determined the right payload via
    ``layer_signed_payload`` / ``canonical_bytes``).

    ``public_key_pem`` returns the PEM-encoded Ed25519 public key that
    the trust root will verify against. Wrappers are responsible for
    fetching this once at construction so per-sign calls don't pay the
    extra round-trip.
    """

    def sign(self, message: bytes) -> bytes: ...

    def public_key_pem(self) -> bytes: ...


class KMSSigningService:
    """HSM-backed :class:`PlatformSigner`. The key never leaves the HSM.

    The class is a thin :class:`PlatformSigner` adapter over any
    :class:`KMSBackend` implementation. Production wires
    :class:`HvacTransitBackend` (Vault Transit; Ed25519 native) or a
    bespoke backend against the operator's HSM. Tests inject a stub
    backend so the staging + reproduction flows can be exercised
    without provisioning real HSM resources.

    Construction:

    ::

        signer = KMSSigningService(backend=HvacTransitBackend(
            client=hvac_client, mount="transit", key_name="darwin-platform",
        ))
    """

    def __init__(self, *, backend: KMSBackend) -> None:
        if not isinstance(backend, KMSBackend):
            raise SigningServiceError(
                "KMSSigningService requires a KMSBackend"
            )
        self._backend = backend
        # Cache the PEM at construction so the trust-root verification path
        # doesn't pay a HSM round-trip every boot.
        try:
            self._pem = bytes(backend.public_key_pem())
        except Exception as exc:
            raise SigningServiceError(
                f"could not fetch HSM public key: {exc}"
            ) from exc
        if not self._pem:
            raise SigningServiceError(
                "HSM backend returned empty public key"
            )

    def sign(self, message: bytes) -> bytes:
        try:
            sig = self._backend.sign(bytes(message))
        except Exception as exc:
            raise SigningServiceError(f"HSM sign failed: {exc}") from exc
        if not sig:
            raise SigningServiceError("HSM backend returned empty signature")
        return bytes(sig)

    def public_key_pem(self) -> bytes:
        return self._pem


class HvacTransitBackend:
    """HashiCorp Vault Transit-engine :class:`KMSBackend`.

    Vault Transit supports Ed25519 natively, so the existing trust root
    in :mod:`darwin_dna.signature` verifies signatures unchanged. The
    private key lives in Vault; we only ever read it as PEM (public
    half) and POST messages to be signed.

    Args:
        client: an ``hvac.Client`` with read+write on the transit mount.
            Tests inject a stub exposing ``secrets.transit.{sign_data,
            read_key}``.
        mount: the Vault Transit mount path (default ``"transit"``).
        key_name: the Transit key name used for the platform signing
            identity (e.g. ``"darwin-platform"``).
        reauth: re-login callable for a leased (Kubernetes/AppRole) token;
            an auth failure calls it once and retries.
    """

    def __init__(
        self,
        *,
        client: Any,
        mount: str = "transit",
        key_name: str,
        reauth: Any | None = None,
    ) -> None:
        if client is None:
            raise SigningServiceError("HvacTransitBackend requires a client")
        if not key_name:
            raise SigningServiceError("HvacTransitBackend requires a key_name")
        self._client = client
        self._mount = mount
        self._key = key_name
        self._reauth = reauth
        self._pem = self._fetch_pem()

    def _call(self, fn: Any) -> Any:
        try:
            return fn()
        except Exception as exc:
            from ..adapters.hvac_vault import HvacVaultClient

            if self._reauth is None or not HvacVaultClient._is_auth_error(exc):
                raise
            self._reauth()
            return fn()

    def _fetch_pem(self) -> bytes:
        """Read the Transit key's latest public-key PEM."""
        try:
            resp = self._call(lambda: self._client.secrets.transit.read_key(
                name=self._key, mount_point=self._mount,
            ))
        except Exception as exc:
            raise SigningServiceError(
                f"Vault read_key failed for {self._key!r}: {exc}"
            ) from exc
        data = (resp or {}).get("data", {}) or {}
        keys = data.get("keys") or {}
        # Transit returns versions keyed by integer-as-string; pick the
        # highest version's public_key. Newer Vault SDKs may return a
        # dict-of-versions; older return only one entry.
        if not keys:
            raise SigningServiceError(
                f"Vault returned no key versions for {self._key!r}"
            )
        latest_key = max(keys, key=lambda k: int(k))
        version = keys[latest_key]
        pem = (
            version.get("public_key")
            if isinstance(version, dict) else None
        )
        if not pem:
            raise SigningServiceError(
                f"Vault key {self._key!r} v{latest_key} has no public_key"
            )
        return pem.encode() if isinstance(pem, str) else bytes(pem)

    def public_key_pem(self) -> bytes:
        return self._pem

    def sign(self, message: bytes) -> bytes:
        # Transit's sign_data accepts base64-encoded input + returns
        # "vault:vN:<base64>" — strip the prefix, decode.
        b64_input = base64.b64encode(bytes(message)).decode()
        try:
            resp = self._call(lambda: self._client.secrets.transit.sign_data(
                name=self._key,
                hash_input=b64_input,
                mount_point=self._mount,
            ))
        except Exception as exc:
            raise SigningServiceError(
                f"Vault sign_data failed: {exc}"
            ) from exc
        data = (resp or {}).get("data", {}) or {}
        sig = data.get("signature")
        if not sig:
            raise SigningServiceError(
                "Vault sign_data returned no signature"
            )
        # Strip the ``vault:vN:`` prefix and base64-decode.
        if isinstance(sig, bytes):
            sig = sig.decode()
        if ":" in sig:
            sig = sig.rsplit(":", 1)[-1]
        try:
            return base64.b64decode(sig)
        except Exception as exc:
            raise SigningServiceError(
                f"could not base64-decode Vault signature: {exc}"
            ) from exc


# -- factory ---------------------------------------------------------------
def build_signer(
    *,
    hvac_client: Any | None = None,
    transit_mount: str = "transit",
    transit_key_name: str | None = None,
    reauth: Any | None = None,
    kms_backend: KMSBackend | None = None,
    raw_private: bytes | None = None,
) -> PlatformSigner:
    """Single construction site for the supervisor's signer.

    Priority order:

    1. ``kms_backend`` (any :class:`KMSBackend`) → :class:`KMSSigningService`.
    2. ``hvac_client`` + ``transit_key_name`` →
       :class:`KMSSigningService` wrapping :class:`HvacTransitBackend`.
    3. Fallback → :class:`LocalEd25519Signer` (optionally restoring
       the persisted ``raw_private``).

    Use the supervisor's CLI flags (``--vault-url``, ``--vault-transit-key``,
    or a programmatic ``kms_backend=``) to choose between them.
    """
    if kms_backend is not None:
        return KMSSigningService(backend=kms_backend)
    if hvac_client is not None and transit_key_name:
        return KMSSigningService(
            backend=HvacTransitBackend(
                client=hvac_client,
                mount=transit_mount,
                key_name=transit_key_name,
                reauth=reauth,
            )
        )
    return LocalEd25519Signer(raw_private=raw_private)
