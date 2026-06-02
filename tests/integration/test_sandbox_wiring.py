"""Wiring tests for the E2B sandbox factory + production fail-closed guard.

doc 14 §sandbox: in production the in-memory FakeSandbox (which returns
ok=True/stdout='' for ANY code without executing it) must be refused — a
missing E2B key in production silently defeats mutation-eval integrity and
neuron isolation, so the supervisor must fail closed rather than fall back.
"""

from __future__ import annotations

import pytest

from darwin_platform.__main__ import _build_real_factories, build_parser

_KEY = "0x" + "11" * 32


def _args(*extra: str):
    return build_parser().parse_args([
        "serve",
        "--wallet-provider", "web3",
        "--chain-networks", "base",
        "--rpc-url", "http://localhost:8545",
        "--wallet-key", _KEY,
        *extra,
    ])


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in (
        "E2B_API_KEY", "DARWIN_ENV", "DARWIN_RPC_URL", "DARWIN_WALLET_KEY",
        "BROWSERBASE_URL", "BROWSERBASE_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)


def test_production_without_e2b_refuses_to_boot(capsys):
    with pytest.raises(SystemExit):
        _build_real_factories(_args("--env", "production"))
    err = capsys.readouterr().err.lower()
    assert "e2b" in err
    assert "production" in err


def test_dev_without_e2b_is_allowed_with_fake():
    factories = _build_real_factories(_args("--env", "dev"))
    # No sandbox factory registered → supervisor keeps the FakeSandbox, which
    # is fine in dev.
    assert "sandbox_factory" not in factories


def test_e2b_key_registers_sandbox_factory():
    factories = _build_real_factories(_args("--e2b-api-key", "sk-test", "--env", "production"))
    assert "sandbox_factory" in factories


def test_mainnet_implies_production_for_sandbox():
    # Real money => production. local-vault is the doc-blessed mainnet wallet
    # path (web3 shared-key is refused earlier), so it reaches the sandbox
    # guard, which must refuse the dev fake on mainnet.
    args = build_parser().parse_args([
        "serve",
        "--wallet-provider", "local-vault",
        "--chain-networks", "base",
        "--rpc-url", "http://localhost:8545",
        "--network", "mainnet",
    ])
    with pytest.raises(SystemExit):
        _build_real_factories(args)
