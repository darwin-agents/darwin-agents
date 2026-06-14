"""Orchestrator pod wires the real LLM transport (doc 13).

The pod's _apply_real_adapters previously installed vault/wallet/browser/
x402/sandbox but NOT the LLM transport, so k8s Darwins silently ran FakeLLM.
These tests lock in that the pod now applies the llm factory, passing its
darwin_id for per-request attribution.
"""

from __future__ import annotations

import types

from darwin_platform.runtime import orchestrator


def test_invoke_pod_llm_factory_passes_darwin_id():
    seen: dict = {}

    def factory(wallet, clock, *, darwin_id=None):
        seen["wallet"] = wallet
        seen["darwin_id"] = darwin_id
        return "transport"

    out = orchestrator._invoke_pod_llm_factory(  # noqa: SLF001
        factory, "W", lambda: 0.0, "darwin-7",
    )
    assert out == "transport"
    assert seen == {"wallet": "W", "darwin_id": "darwin-7"}


def test_invoke_pod_llm_factory_supports_two_arg_factory():
    def factory(wallet, clock):  # no darwin_id kwarg
        return ("ok", wallet)

    out = orchestrator._invoke_pod_llm_factory(  # noqa: SLF001
        factory, "W", lambda: 0.0, "darwin-7",
    )
    assert out == ("ok", "W")


def _fake_args():
    # _apply_real_adapters reads these attributes directly off args to build
    # the pod's fake_args; supply them all as None/defaults.
    return types.SimpleNamespace(
        wallet_provider="web3",
        cdp_api_key_id=None, cdp_api_key_secret=None, cdp_wallet_secret=None,
        cdp_mainnet=False, rpc_url=None, wallet_key=None, usdc_contract=None,
        chain_networks=None, solana_rpc_url=None, solana_wallet_key=None,
        stellar_horizon_url=None, stellar_secret=None,
        vault_url=None, vault_token=None, vault_mount=None,
        browserbase_url=None, browserbase_api_key=None, e2b_api_key=None,
    )


def test_apply_real_adapters_installs_llm_transport(monkeypatch):
    from darwin_fakes import FakeStack

    recorded: dict = {}

    def _fake_factory(wallet, clock, *, darwin_id=None):
        recorded["darwin_id"] = darwin_id
        return "LLM-TRANSPORT"

    # Return only the llm factory so the test is isolated from wallet/vault.
    from darwin_platform import __main__ as cli_main
    monkeypatch.setattr(
        cli_main, "_build_real_factories",
        lambda _args: {"llm_transport_factory": _fake_factory},
    )

    stack = FakeStack(opening_usdc=100.0, clock=lambda: 0.0)
    orchestrator._apply_real_adapters(  # noqa: SLF001
        stack, _fake_args(), lambda: 0.0, darwin_id="darwin-abc",
    )
    assert stack.llm == "LLM-TRANSPORT"
    assert recorded["darwin_id"] == "darwin-abc"


def test_apply_real_adapters_no_llm_factory_leaves_stack_llm(monkeypatch):
    from darwin_fakes import FakeStack
    from darwin_platform import __main__ as cli_main

    monkeypatch.setattr(
        cli_main, "_build_real_factories", lambda _args: {},
    )
    stack = FakeStack(opening_usdc=100.0, clock=lambda: 0.0)
    original_llm = stack.llm
    orchestrator._apply_real_adapters(  # noqa: SLF001
        stack, _fake_args(), lambda: 0.0, darwin_id="darwin-abc",
    )
    # No real factory → the deterministic FakeLLM stays in place.
    assert stack.llm is original_llm


def test_apply_real_adapters_returns_pod_money_manifest(monkeypatch):
    """The pod reports its own money rails: wallet + x402 from the factories it
    wired, network/chains from the forwarded flags. Platform-mediated rails
    (fiat/sanctions/kyc/omnibus) are not run in-pod, so they report none."""
    from darwin_fakes import FakeStack
    from darwin_platform import __main__ as cli_main

    monkeypatch.setattr(
        cli_main, "_build_real_factories",
        lambda _args: {
            "wallet_factory": lambda _id, _clk, _vault: object(),
            "x402_factory": lambda _wallet: object(),
        },
    )
    args = _fake_args()
    args.wallet_provider = "cdp"
    args.cdp_mainnet = True
    args.chain_networks = "base,solana"

    stack = FakeStack(opening_usdc=100.0, clock=lambda: 0.0)
    manifest = orchestrator._apply_real_adapters(  # noqa: SLF001
        stack, args, lambda: 0.0, darwin_id="darwin-abc",
    )

    assert manifest["network"] == "base-mainnet"
    assert manifest["mainnet"] is True
    assert manifest["wallet"] == {
        "provider": "cdp", "live": True, "chains": ["base", "solana"],
    }
    assert manifest["x402"] == {"live": True}
    # Platform-mediated rails are not wired in-pod.
    assert manifest["fiat"] == {"provider": "none", "live": False}
    assert manifest["sanctions"] == {"provider": "none", "live": False}
    assert manifest["kyc"] == {"provider": "none", "live": False}
    assert manifest["omnibus"] == {"enabled": False}


def test_apply_real_adapters_no_wallet_reports_wallet_not_live(monkeypatch):
    """No wallet factory → wallet rail collapses to none/not-live even though a
    provider string was forwarded."""
    from darwin_fakes import FakeStack
    from darwin_platform import __main__ as cli_main

    monkeypatch.setattr(cli_main, "_build_real_factories", lambda _args: {})
    args = _fake_args()
    args.wallet_provider = "cdp"

    stack = FakeStack(opening_usdc=100.0, clock=lambda: 0.0)
    manifest = orchestrator._apply_real_adapters(  # noqa: SLF001
        stack, args, lambda: 0.0, darwin_id="darwin-abc",
    )
    assert manifest["wallet"] == {
        "provider": "none", "live": False, "chains": ["base"],
    }
    assert manifest["network"] == "base-sepolia"
    assert manifest["x402"] == {"live": False}


def test_apply_real_adapters_forwards_platform_money_rails(monkeypatch):
    """When the supervisor forwards DARWIN_PLATFORM_MONEY_RAILS, the pod folds
    those platform-mediated rails (fiat/sanctions/kyc/omnibus) into its manifest
    so the planner sees the full picture — while wallet/x402/network stay
    pod-local truth."""
    import json

    from darwin_fakes import FakeStack
    from darwin_platform import __main__ as cli_main

    monkeypatch.setattr(
        cli_main, "_build_real_factories",
        lambda _args: {"wallet_factory": lambda _id, _clk, _vault: object()},
    )
    monkeypatch.setenv(
        "DARWIN_PLATFORM_MONEY_RAILS",
        json.dumps({
            "fiat": {"provider": "stripe", "live": True},
            "sanctions": {"provider": "chainalysis", "live": True},
            "kyc": {"provider": "persona", "live": True},
            "omnibus": {"enabled": True},
        }),
    )
    args = _fake_args()
    args.wallet_provider = "cdp"

    stack = FakeStack(opening_usdc=100.0, clock=lambda: 0.0)
    manifest = orchestrator._apply_real_adapters(  # noqa: SLF001
        stack, args, lambda: 0.0, darwin_id="darwin-abc",
    )

    assert manifest["fiat"] == {"provider": "stripe", "live": True}
    assert manifest["sanctions"] == {"provider": "chainalysis", "live": True}
    assert manifest["kyc"] == {"provider": "persona", "live": True}
    assert manifest["omnibus"] == {"enabled": True}
    # Wallet rail stays pod-local truth (a real factory was wired).
    assert manifest["wallet"]["live"] is True


def test_apply_real_adapters_ignores_malformed_money_rails(monkeypatch):
    """A malformed DARWIN_PLATFORM_MONEY_RAILS must not crash boot — the pod
    falls back to the pod-local "none" vantage."""
    from darwin_fakes import FakeStack
    from darwin_platform import __main__ as cli_main

    monkeypatch.setattr(cli_main, "_build_real_factories", lambda _args: {})
    monkeypatch.setenv("DARWIN_PLATFORM_MONEY_RAILS", "{not valid json")
    args = _fake_args()

    stack = FakeStack(opening_usdc=100.0, clock=lambda: 0.0)
    manifest = orchestrator._apply_real_adapters(  # noqa: SLF001
        stack, args, lambda: 0.0, darwin_id="darwin-abc",
    )
    assert manifest["fiat"] == {"provider": "none", "live": False}
    assert manifest["omnibus"] == {"enabled": False}
