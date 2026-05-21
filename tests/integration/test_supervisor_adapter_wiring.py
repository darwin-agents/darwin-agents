"""Tests for real-adapter factory injection (plan task #1 + #2).

Verifies that:
  - Supervisor.spawn() replaces FakeWallet / FakeVault / FakeBrowser /
    FakeX402 when the corresponding factory kwargs are provided.
  - The x402_client reaches the NeuronContext of outbound-web neurons but
    NOT neurons that have no web capability tags.
  - The CLI's _build_real_factories returns the right kwargs from args.
  - Platform.x402_client is forwarded to NeuronContext.http via
    DarwinRuntime._build_neuron_context.

All tests are fully offline — no real RPC, Vault, or Browserbase calls.
"""

from __future__ import annotations

import asyncio
import time
import types

import pytest

from darwin_fakes import FakeStack, make_platform
from darwin_dna.evolution.loop import Platform
from darwin_dna.neurons._base import NeuronContext


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

class _StubWallet:
    """Minimal WalletPort for factory-replacement tests."""
    name = "stub_wallet"

    def balance_usdc(self): return 999.0
    def spent_today_usdc(self): return 0.0
    def spent_lifetime_usdc(self): return 0.0
    def recent_spend_usdc(self): return ()
    def pay(self, *, to, amount_usdc, memo): return {"tx_hash": "0xstub"}
    def debit(self, amount, memo): pass
    def movements(self): return []
    def owner_soft_caps(self): return None
    def seconds_since_revenue(self): return 0.0


class _StubVault:
    """Minimal RawVaultPort for factory-replacement tests."""
    name = "stub_vault"

    def put(self, key, value): pass
    def get(self, key): return None
    def delete(self, key): pass


class _StubBrowser:
    name = "stub_browser"

    def run(self, steps): return {"ok": True, "data": {}, "cost_usdc": 0.01}


class _StubX402:
    name = "stub_x402"

    def request(self, method, url, **kwargs):
        return types.SimpleNamespace(status_code=200, headers={})


class _StubSandbox:
    name = "stub_sandbox"

    def run_code(self, *, language, code, timeout_seconds=30.0):
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": "",
                "cost_usdc": 0.0}


# ---------------------------------------------------------------------------
# 1. FakeStack.x402_client defaults to None
# ---------------------------------------------------------------------------

def test_fakestack_x402_client_default_none():
    stack = FakeStack(opening_usdc=10.0, clock=time.time)
    assert stack.x402_client is None


def test_fakestack_platform_passes_x402_client():
    stack = FakeStack(opening_usdc=10.0, clock=time.time)
    stub = _StubX402()
    stack.x402_client = stub
    p = stack.platform()
    assert p.x402_client is stub


# ---------------------------------------------------------------------------
# 2. Platform stores x402_client
# ---------------------------------------------------------------------------

def test_platform_x402_client_kwarg():
    _, stack = make_platform()
    p = stack.platform()
    assert p.x402_client is None  # default

    stub = _StubX402()
    stack.x402_client = stub
    p2 = stack.platform()
    assert p2.x402_client is stub


# ---------------------------------------------------------------------------
# 3. NeuronContext has http field defaulting to None
# ---------------------------------------------------------------------------

def test_neuron_context_http_default():
    from darwin_fakes.fake_llm import FakeLLM
    from darwin_fakes.fake_wallet import FakeWallet
    wallet = FakeWallet(opening_usdc=10.0, clock=time.time)
    llm = FakeLLM(wallet=wallet)
    ctx = NeuronContext(darwin_id="test", llm=llm, audit=object())
    assert ctx.http is None


def test_neuron_context_http_kwarg():
    from darwin_fakes.fake_llm import FakeLLM
    from darwin_fakes.fake_wallet import FakeWallet
    wallet = FakeWallet(opening_usdc=10.0, clock=time.time)
    llm = FakeLLM(wallet=wallet)
    stub = _StubX402()
    ctx = NeuronContext(darwin_id="test", llm=llm, audit=object(), http=stub)
    assert ctx.http is stub


# ---------------------------------------------------------------------------
# 4. x402 client reaches browser.operator neuron context but not others
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_x402_granted_to_web_neuron():
    """browser.operator has 'web.operate' tag — should receive http."""
    from darwin_dna.evolution import boot

    stub_x402 = _StubX402()
    _, stack = make_platform()
    stack.x402_client = stub_x402
    platform = stack.platform()
    runtime = boot(platform)

    nctx = runtime._build_neuron_context("browser.operator")
    assert nctx.http is stub_x402


@pytest.mark.asyncio
async def test_x402_not_granted_to_non_web_neuron():
    """content.writer has no 'web.*' tags — should NOT receive http."""
    from darwin_dna.evolution import boot

    stub_x402 = _StubX402()
    _, stack = make_platform()
    stack.x402_client = stub_x402
    platform = stack.platform()
    runtime = boot(platform)

    nctx = runtime._build_neuron_context("content.writer")
    assert nctx.http is None


@pytest.mark.asyncio
async def test_x402_absent_when_platform_has_none():
    """No x402_client on platform → http is None for all neurons."""
    from darwin_dna.evolution import boot

    _, stack = make_platform()
    assert stack.x402_client is None
    runtime = boot(stack.platform())

    nctx = runtime._build_neuron_context("browser.operator")
    assert nctx.http is None


# ---------------------------------------------------------------------------
# 5. Supervisor factory kwargs replace the fake stack components
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_supervisor_wallet_factory_replaces_fake():
    from darwin_platform.runtime.supervisor import Supervisor

    stub = _StubWallet()

    def wallet_factory(clock):
        return stub

    sup = Supervisor(wallet_factory=wallet_factory)
    status = await sup.spawn(opening_usdc=50.0)
    darwin_id = status.darwin_id

    handle = sup._darwins[darwin_id]
    assert handle.stack.wallet is stub

    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_vault_factory_replaces_fake():
    from darwin_platform.runtime.supervisor import Supervisor

    stub = _StubVault()

    def vault_factory():
        return stub

    sup = Supervisor(vault_factory=vault_factory)
    status = await sup.spawn(opening_usdc=50.0)
    darwin_id = status.darwin_id

    handle = sup._darwins[darwin_id]
    assert handle.stack.vault is stub

    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_browser_factory_replaces_fake():
    from darwin_platform.runtime.supervisor import Supervisor

    stub = _StubBrowser()

    def browser_factory(wallet):
        return stub

    # browser_cache_enabled=False + resource_throttle_enabled=False so the
    # stub is the direct substrate; wrapping behaviour is exercised in
    # test_browser_cache.py and test_resource_throttle.py.
    sup = Supervisor(
        browser_factory=browser_factory,
        browser_cache_enabled=False,
        resource_throttle_enabled=False,
    )
    status = await sup.spawn(opening_usdc=50.0)
    darwin_id = status.darwin_id

    handle = sup._darwins[darwin_id]
    assert handle.stack.browser is stub

    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_x402_factory_sets_stack_client():
    from darwin_platform.runtime.supervisor import Supervisor

    stub = _StubX402()

    def x402_factory(wallet):
        return stub

    sup = Supervisor(x402_factory=x402_factory, resource_throttle_enabled=False)
    status = await sup.spawn(opening_usdc=50.0)
    darwin_id = status.darwin_id

    handle = sup._darwins[darwin_id]
    assert handle.stack.x402_client is stub

    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_sandbox_factory_replaces_fake():
    from darwin_platform.runtime.supervisor import Supervisor

    stub = _StubSandbox()

    def sandbox_factory(wallet):
        return stub

    sup = Supervisor(sandbox_factory=sandbox_factory, resource_throttle_enabled=False)
    status = await sup.spawn(opening_usdc=50.0)
    darwin_id = status.darwin_id

    handle = sup._darwins[darwin_id]
    assert handle.stack.sandbox is stub

    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_all_factories_together():
    """All five factories applied together; each stub is the live component."""
    from darwin_platform.runtime.supervisor import Supervisor

    w = _StubWallet()
    v = _StubVault()
    b = _StubBrowser()
    x = _StubX402()
    s = _StubSandbox()

    sup = Supervisor(
        wallet_factory=lambda clock: w,
        vault_factory=lambda: v,
        browser_factory=lambda wallet: b,
        x402_factory=lambda wallet: x,
        sandbox_factory=lambda wallet: s,
        browser_cache_enabled=False,
        resource_throttle_enabled=False,
    )
    status = await sup.spawn(opening_usdc=50.0)
    darwin_id = status.darwin_id
    handle = sup._darwins[darwin_id]

    assert handle.stack.wallet is w
    assert handle.stack.vault is v
    assert handle.stack.browser is b
    assert handle.stack.x402_client is x
    assert handle.stack.sandbox is s

    await sup.shutdown(kill_running=True)


# ---------------------------------------------------------------------------
# 6. CLI _build_real_factories returns correct keys
# ---------------------------------------------------------------------------

def _make_args(**kwargs):
    """Build a minimal argparse.Namespace mirroring serve args."""
    defaults = dict(
        rpc_url=None, wallet_key=None, usdc_contract=None,
        chain_networks="base",
        solana_rpc_url=None, solana_wallet_key=None,
        stellar_horizon_url=None, stellar_secret=None,
        vault_url=None, vault_token=None, vault_mount="secret",
        browserbase_url=None, browserbase_api_key=None,
        e2b_api_key=None,
    )
    defaults.update(kwargs)
    import argparse
    return argparse.Namespace(**defaults)


def test_build_factories_empty_when_no_args():
    from darwin_platform.__main__ import _build_real_factories
    result = _build_real_factories(_make_args())
    assert result == {}


def test_build_factories_wallet_when_rpc_and_key():
    from darwin_platform.__main__ import _build_real_factories
    result = _build_real_factories(_make_args(
        rpc_url="https://sepolia.base.org",
        wallet_key="0xdeadbeef",
    ))
    assert "wallet_factory" in result
    assert "x402_factory" in result      # auto-wired when wallet is real
    assert "vault_factory" not in result
    assert "browser_factory" not in result


def test_build_factories_vault_when_url_and_token():
    from darwin_platform.__main__ import _build_real_factories
    result = _build_real_factories(_make_args(
        vault_url="https://vault.example.com:8200",
        vault_token="s.abc",
    ))
    assert "vault_factory" in result
    assert "wallet_factory" not in result


def test_build_factories_browser_when_api_key():
    from darwin_platform.__main__ import _build_real_factories
    result = _build_real_factories(_make_args(
        browserbase_api_key="bb-key-123",
    ))
    assert "browser_factory" in result


def test_build_factories_sandbox_when_e2b_key():
    from darwin_platform.__main__ import _build_real_factories
    result = _build_real_factories(_make_args(
        e2b_api_key="e2b-abc",
    ))
    assert "sandbox_factory" in result
    assert "wallet_factory" not in result


def test_build_factories_all_present():
    from darwin_platform.__main__ import _build_real_factories
    result = _build_real_factories(_make_args(
        rpc_url="https://sepolia.base.org",
        wallet_key="0xdeadbeef",
        vault_url="https://vault.example.com:8200",
        vault_token="s.abc",
        browserbase_api_key="bb-key-123",
        e2b_api_key="e2b-abc",
    ))
    assert "wallet_factory" in result
    assert "vault_factory" in result
    assert "browser_factory" in result
    assert "x402_factory" in result
    assert "sandbox_factory" in result


# ---------------------------------------------------------------------------
# 7. CLI argument parsing smoke-test
# ---------------------------------------------------------------------------

def test_cli_parse_serve_real_flags():
    from darwin_platform.__main__ import build_parser
    parser = build_parser()
    args = parser.parse_args([
        "serve",
        "--rpc-url", "https://sepolia.base.org",
        "--wallet-key", "0xdeadbeef",
        "--vault-url", "https://vault.example.com:8200",
        "--vault-token", "s.abc",
        "--vault-mount", "darwin",
        "--browserbase-api-key", "bb-123",
        "--e2b-api-key", "e2b-xyz",
    ])
    assert args.rpc_url == "https://sepolia.base.org"
    assert args.wallet_key == "0xdeadbeef"
    assert args.vault_url == "https://vault.example.com:8200"
    assert args.vault_token == "s.abc"
    assert args.vault_mount == "darwin"
    assert args.browserbase_api_key == "bb-123"
    assert args.e2b_api_key == "e2b-xyz"
