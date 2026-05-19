"""The trust root: signature verification (docs/engineering/18, "the trust root").

Every DNA layer and the manifest binding them are signed by the platform's
signing key. That private key lives in an HSM/KMS and is *never* present in any
Darwin's sandbox — the genome only ever holds the public key and only ever
*verifies*. A failed verification halts the Darwin before any side-effecting
code runs.

We use Ed25519: small keys, deterministic signatures, no parameter choices to
get wrong, constant-time verification in ``cryptography``.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key


class SignatureError(Exception):
    """Verification failed. The caller MUST refuse to boot/act. Never caught
    and swallowed inside the trust root."""


def load_platform_public_key(pem: bytes) -> Ed25519PublicKey:
    """Parse the platform's published Ed25519 public key (PEM).

    Anything that isn't an Ed25519 public key is a configuration error, not a
    soft failure — reject it loudly so a wrong-curve key can never silently
    "verify" nothing.
    """
    try:
        key = load_pem_public_key(pem)
    except Exception as exc:  # malformed PEM, truncation, garbage
        raise SignatureError(f"unreadable platform public key: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise SignatureError(
            f"platform key must be Ed25519, got {type(key).__name__}"
        )
    return key


def verify(public_key: Ed25519PublicKey, message: bytes, signature: bytes) -> None:
    """Raise :class:`SignatureError` unless ``signature`` is the platform's
    signature over exactly ``message``. No return value: there is no
    "verified-ish". Either it raises or the bytes are authentic.
    """
    if not isinstance(message, (bytes, bytearray)):
        raise SignatureError("message to verify must be bytes")
    if not signature:
        raise SignatureError("empty signature")
    try:
        public_key.verify(bytes(signature), bytes(message))
    except InvalidSignature as exc:
        raise SignatureError("signature does not verify") from exc
