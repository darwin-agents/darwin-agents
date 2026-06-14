"""Unit tests for ``Supervisor.money_capabilities()`` — the money-rail manifest
fed to each Darwin's planner and surfaced on the operator console. It must
report a rail ``live`` only when a *real* adapter is wired, and degrade to
fake/offline providers otherwise, derived purely from how the supervisor was
constructed."""

from __future__ import annotations

from darwin_platform.adapters.fiat import StubFiatRamp
from darwin_platform.payments.sanctions import (
    CachedScreener,
    ChainalysisScreener,
    LocalDenylistScreener,
)
from darwin_platform.runtime.supervisor import Supervisor
from darwin_platform.security.kyc import KYCService, StubKYCProvider


def test_all_fake_deployment_reports_nothing_live() -> None:
    sup = Supervisor()
    caps = sup.money_capabilities()

    assert caps["network"] == "base-sepolia"
    assert caps["mainnet"] is False
    assert caps["wallet"] == {"provider": "none", "live": False, "chains": ["base"]}
    assert caps["fiat"] == {"provider": "none", "live": False}
    assert caps["sanctions"] == {"provider": "none", "live": False}
    assert caps["kyc"] == {"provider": "none", "live": False}
    assert caps["omnibus"] == {"enabled": False}
    assert caps["x402"] == {"live": False}


def test_descriptors_and_real_rails_are_reported_live() -> None:
    # A real wallet factory + mainnet + a real (cache-wrapped) sanctions
    # screener, alongside a stub fiat ramp and stub KYC.
    screener = CachedScreener(ChainalysisScreener(api_key="test-key"))
    sup = Supervisor(
        wallet_factory=lambda *a, **k: object(),
        wallet_provider="cdp",
        network="base-mainnet",
        chain_networks=("base", "solana"),
        x402_factory=lambda wallet: object(),
        fiat_ramp=StubFiatRamp(),
        sanctions_screener=screener,
        kyc_service=KYCService(provider=StubKYCProvider()),
    )
    caps = sup.money_capabilities()

    assert caps["network"] == "base-mainnet"
    assert caps["mainnet"] is True
    assert caps["wallet"] == {
        "provider": "cdp",
        "live": True,
        "chains": ["base", "solana"],
    }
    # Stub ramp is wired but not a real money rail.
    assert caps["fiat"] == {"provider": "stub", "live": False}
    # Real screener, even though cache-wrapped, is unwrapped and reported live.
    assert caps["sanctions"] == {"provider": "chainalysis", "live": True}
    assert caps["kyc"] == {"provider": "stub", "live": False}
    assert caps["x402"] == {"live": True}


def test_local_denylist_is_not_live() -> None:
    sup = Supervisor(sanctions_screener=LocalDenylistScreener(denylist=set()))
    assert sup.money_capabilities()["sanctions"] == {
        "provider": "local",
        "live": False,
    }
