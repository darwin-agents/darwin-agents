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
