"""Security: auth, KYC, signing, watermarking (docs/engineering/02, 09, 22).

:mod:`auth` is the HTTP bearer-token surface every API route consumes.
:mod:`kyc` gates fund/spawn/withdraw on identity verification. The
HSM-backed :mod:`signing_service` seals DNA layers and verifies on
boot. :mod:`key_rotation` ages signing keys with a drain window.
:mod:`watermark` embeds HMAC sidecars in vault uploads for forensic
provenance.
"""

from __future__ import annotations

from .auth import (
    OWNER,
    PLATFORM_OPERATOR,
    AuthContext,
    InMemoryTokenStore,
    TokenStore,
    disabled_context,
)
from .key_rotation import KeyRotationError, KeyRotationService, RetiredKey
from .kyc import (
    KYCError,
    KYCProvider,
    KYCService,
    KYCStatus,
    KYCTier,
    KYCVerdict,
    PersonaKYCProvider,
    StubKYCProvider,
)
from .signing_service import (
    HvacTransitBackend,
    KMSBackend,
    KMSSigningService,
    LocalEd25519Signer,
    PlatformSigner,
    SigningServiceError,
    build_signer,
)
from .watermark import (
    HmacWatermarker,
    NoOpWatermarker,
    WatermarkRecord,
    Watermarker,
    verify as verify_watermark,
)

__all__ = [
    "AuthContext",
    "HmacWatermarker",
    "HvacTransitBackend",
    "InMemoryTokenStore",
    "KMSBackend",
    "KMSSigningService",
    "KYCError",
    "KYCProvider",
    "KYCService",
    "KYCStatus",
    "KYCTier",
    "KYCVerdict",
    "KeyRotationError",
    "KeyRotationService",
    "LocalEd25519Signer",
    "NoOpWatermarker",
    "OWNER",
    "PLATFORM_OPERATOR",
    "PersonaKYCProvider",
    "PlatformSigner",
    "RetiredKey",
    "SigningServiceError",
    "StubKYCProvider",
    "TokenStore",
    "WatermarkRecord",
    "Watermarker",
    "build_signer",
    "disabled_context",
    "verify_watermark",
]
