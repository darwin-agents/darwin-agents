"""Money-capabilities manifest — which payment rails a deployment actually has
wired, each with a ``live`` flag distinguishing a real adapter from a
fake/stub/offline fallback (env may be set yet the adapter still a stub).

One shared shape so both run paths report identically:
- the in-process :class:`Supervisor` (introspecting its constructed handles), and
- the per-Darwin k8s orchestrator pod (introspecting the adapters it wired).

The same dict is fed to each Darwin's planner (via ``Platform.money_capabilities``)
so the genome reasons over real rail availability, and surfaced read-only on the
operator console.
"""

from __future__ import annotations

from typing import Any


def detect_fiat(ramp: Any | None) -> dict[str, Any]:
    """Fiat ramp provider + live flag. The ``name`` tag is part of the
    ``FiatRamp`` protocol (``stub``/``stripe``/``moonpay``); only the real
    ramps move money."""
    if ramp is None:
        return {"provider": "none", "live": False}
    name = getattr(ramp, "name", "stub")
    return {"provider": name, "live": name in ("stripe", "moonpay")}


def detect_sanctions(screener: Any | None) -> dict[str, Any]:
    """Sanctions screener provider + live flag. Real screeners may be wrapped
    in a :class:`CachedScreener`, so unwrap before classifying. An offline
    denylist is wired but not a live screener."""
    from .sanctions import (
        AllowAllScreener,
        CachedScreener,
        ChainalysisScreener,
        LocalDenylistScreener,
        TRMScreener,
    )

    if isinstance(screener, CachedScreener):
        screener = screener._inner  # noqa: SLF001
    if screener is None:
        return {"provider": "none", "live": False}
    if isinstance(screener, ChainalysisScreener):
        return {"provider": "chainalysis", "live": True}
    if isinstance(screener, TRMScreener):
        return {"provider": "trm", "live": True}
    if isinstance(screener, (LocalDenylistScreener, AllowAllScreener)):
        return {"provider": "local", "live": False}
    return {"provider": "unknown", "live": False}


def detect_kyc(kyc_service: Any | None) -> dict[str, Any]:
    """KYC provider + live flag. The variant lives on the service's
    ``provider`` object; the stub is wired but not a live verifier."""
    from ..security.kyc import OnfidoKYCProvider, PersonaKYCProvider

    if kyc_service is None:
        return {"provider": "none", "live": False}
    provider = getattr(kyc_service, "provider", None)
    if isinstance(provider, PersonaKYCProvider):
        return {"provider": "persona", "live": True}
    if isinstance(provider, OnfidoKYCProvider):
        return {"provider": "onfido", "live": True}
    return {"provider": "stub", "live": False}


def assemble_money_capabilities(
    *,
    network: str,
    wallet_provider: str,
    wallet_live: bool,
    chains: list[str],
    fiat: dict[str, Any],
    sanctions: dict[str, Any],
    kyc: dict[str, Any],
    omnibus_enabled: bool,
    x402_live: bool,
) -> dict[str, Any]:
    """Assemble the canonical manifest dict from already-resolved per-rail
    values. Keeping the shape here means the Supervisor and the pod can't
    drift. ``wallet.provider`` collapses to ``"none"`` when no real wallet is
    wired so a stale provider string can't imply a live rail."""
    return {
        "network": network,
        "mainnet": "mainnet" in network,
        "wallet": {
            "provider": wallet_provider if wallet_live else "none",
            "live": wallet_live,
            "chains": list(chains),
        },
        "fiat": fiat,
        "sanctions": sanctions,
        "kyc": kyc,
        "omnibus": {"enabled": omnibus_enabled},
        "x402": {"live": x402_live},
    }
