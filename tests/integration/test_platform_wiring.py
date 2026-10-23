"""Tests for the Phase 1/2 service wiring (docs/engineering/21, 22).

Covers the gap between "a service is built" and "`darwin-platform serve`
can actually turn it on": the CLI-flag → Supervisor-kwarg mapping, the
LiteLLM transport factory, the OTLP traces-URL helper, and the
drive-loop maintenance cadence that actually *runs* drift profiling +
Merkle anchoring on a wall-clock interval.

The OTLP exporter install sets a process-global TracerProvider, so that
path is verified out-of-process in ``test_serve_e2e.py``; here we only
unit-test the pure URL helper.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from darwin_platform.__main__ import (
    _build_platform_services,
    _build_real_factories,
    build_parser,
)
from darwin_platform.observability.drift_detector import DriftDetector
from darwin_platform.observability.merkle_anchor import (
    MerkleAnchorPublisher,
    StubAnchorBackend,
)
from darwin_platform.observability.tracing import otlp_traces_url
from darwin_platform.runtime.supervisor import Supervisor


@pytest.fixture(autouse=True)
def _isolate_provider_env(monkeypatch):
    """Hermetic env for the CLI-wiring tests.

    ``import litellm`` runs ``dotenv.load_dotenv()`` at import time, leaking
    the repo .env into os.environ for whatever test runs after it. Because
    ``_build_real_factories`` reads ``args.X or os.environ.get(...)``, that
    makes these tests depend on collection order (the source of an
    intermittent CI flake). Clear every provider-ish var so the tests depend
    only on their explicit args; monkeypatch restores them afterwards."""
    import os

    prefixes = (
        "DARWIN_", "CDP_", "BROWSERBASE", "BROWSER_USE", "E2B_",
        "STRIPE_", "MOONPAY_", "ONFIDO_", "TWILIO_", "CLERK_", "OTEL_",
    )
    for key in list(os.environ):
        if key.startswith(prefixes):
            monkeypatch.delenv(key, raising=False)


def _serve_args(*extra: str):
    # The CLI's --wallet-provider default is "cdp" (step 2), which requires
    # CDP credentials and sys.exit(2)s without them. Tests in this file are
    # not exercising wallet behaviour, so pin to the shared-key path so a
    # bare `serve` parses cleanly. Tests that need a different provider
    # pass --wallet-provider in `extra` (argparse keeps the last value).
    return build_parser().parse_args(["serve", "--wallet-provider", "web3", *extra])


# ---------------------------------------------------------------------------
# OTLP traces-URL helper
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("http://jaeger:4318", "http://jaeger:4318/v1/traces"),
        ("http://jaeger:4318/", "http://jaeger:4318/v1/traces"),
        ("http://jaeger:4318/v1/traces", "http://jaeger:4318/v1/traces"),
        ("https://otel.example.com", "https://otel.example.com/v1/traces"),
    ],
)
def test_otlp_traces_url_normalisation(endpoint, expected):
    assert otlp_traces_url(endpoint) == expected


# ---------------------------------------------------------------------------
# _build_platform_services — CLI flag → Supervisor kwarg
# ---------------------------------------------------------------------------

def test_platform_services_default_is_just_maintenance_interval():
    svc = _build_platform_services(_serve_args())
    # Nothing enabled by default — only the maintenance cadence is always
    # present (it has a default).
    assert set(svc) == {"maintenance_interval_seconds"}
    assert svc["maintenance_interval_seconds"] == 86400.0


def test_platform_services_drift_and_merkle_flags():
    svc = _build_platform_services(
        _serve_args("--enable-drift-detection", "--enable-merkle-anchor")
    )
    assert isinstance(svc["drift_detector"], DriftDetector)
    assert isinstance(svc["merkle_anchor_publisher"], MerkleAnchorPublisher)


def test_platform_services_kyc_stub():
    svc = _build_platform_services(_serve_args("--kyc-provider", "stub"))
    assert "kyc_service" in svc
    # The stub provider default-approves.
    from darwin_platform.security.kyc import KYCStatus

    out = svc["kyc_service"].start_inquiry(subject="alice")
    verdict = svc["kyc_service"].refresh_status(
        subject="alice", inquiry_id=out["inquiry_id"],
    )
    assert verdict.status == KYCStatus.APPROVED


def test_platform_services_kyc_persona_requires_credentials(capsys):
    with pytest.raises(SystemExit):
        _build_platform_services(_serve_args("--kyc-provider", "persona"))
    assert "persona" in capsys.readouterr().err.lower()


def test_platform_services_maintenance_interval_override():
    svc = _build_platform_services(
        _serve_args("--maintenance-interval", "30")
    )
    assert svc["maintenance_interval_seconds"] == 30.0


def test_platform_services_omnibus_requires_chain(capsys):
    with pytest.raises(SystemExit):
        _build_platform_services(_serve_args("--enable-omnibus"))
    assert "chain" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# LiteLLM transport factory
# ---------------------------------------------------------------------------

def test_litellm_factory_absent_without_flag():
    factories = _build_real_factories(_serve_args())
    assert "llm_transport_factory" not in factories


def test_litellm_factory_builds_transport():
    factories = _build_real_factories(
        _serve_args("--litellm-url", "http://litellm:4000")
    )
    assert "llm_transport_factory" in factories
    # The factory builds a LiteLLMTransport bound to the proxy. Construct
    # it with a stub wallet — no network call happens at construction.
    from darwin_platform.adapters.litellm_transport import LiteLLMTransport

    transport = factories["llm_transport_factory"](
        types.SimpleNamespace(), lambda: 0.0,
    )
    assert isinstance(transport, LiteLLMTransport)


def test_virtual_keys_absent_without_flag():
    svc = _build_platform_services(
        _serve_args("--litellm-url", "http://litellm:4000")
    )
    assert "llm_key_minter" not in svc
    assert "llm_spend_reconciler" not in svc


def test_virtual_keys_wired_with_flag():
    from darwin_platform.adapters import (
        LiteLLMAdminClient,
        LiteLLMSpendReconciler,
    )

    svc = _build_platform_services(
        _serve_args(
            "--litellm-url", "http://litellm:4000",
            "--litellm-master-key", "sk-admin",
            "--litellm-virtual-keys",
            "--litellm-virtual-key-budget-usdc", "30",
            "--litellm-spend-tolerance-usdc", "0.02",
        )
    )
    assert isinstance(svc["llm_key_minter"], LiteLLMAdminClient)
    reconciler = svc["llm_spend_reconciler"]
    assert isinstance(reconciler, LiteLLMSpendReconciler)
    assert reconciler.tolerance_usdc == 0.02
    assert svc["llm_virtual_key_options"] == {"max_budget": 30.0}


def test_virtual_keys_require_litellm_url():
    with pytest.raises(SystemExit):
        _build_platform_services(
            _serve_args("--litellm-virtual-keys", "--litellm-master-key", "k")
        )


def test_virtual_keys_factory_accepts_darwin_id_and_key():
    import inspect

    factories = _build_real_factories(
        _serve_args("--litellm-url", "http://litellm:4000")
    )
    params = inspect.signature(factories["llm_transport_factory"]).parameters
    # The factory must accept the per-Darwin args the supervisor threads in.
    assert "darwin_id" in params
    assert "virtual_key" in params


# ---------------------------------------------------------------------------
# Episodic-memory factory — metered embeddings (closes the bypass gap)
# ---------------------------------------------------------------------------

def test_episodic_factory_absent_without_flag():
    factories = _build_real_factories(_serve_args())
    assert "episodic_memory_factory" not in factories


def test_episodic_factory_signature_accepts_wallet_and_key(tmp_path):
    import inspect

    factories = _build_real_factories(
        _serve_args(
            "--episodic-memory", "lancedb",
            "--lancedb-path", str(tmp_path),
            "--litellm-url", "http://litellm:4000",
        )
    )
    assert "episodic_memory_factory" in factories
    params = inspect.signature(factories["episodic_memory_factory"]).parameters
    # The supervisor threads these in so embedding spend is metered + attributed.
    assert "wallet" in params
    assert "virtual_key" in params


def test_episodic_embed_meters_wallet(tmp_path, monkeypatch):
    """An embedding call debits the Darwin's wallet under `llm:embedding`
    (at the marked-up rate), so episodic-memory spend is billed + reconciled
    instead of silently incurred at the proxy."""
    pytest.importorskip("lancedb")
    import litellm

    # Stub the proxy round-trip: a fixed vector + a known provider cost.
    monkeypatch.setattr(
        litellm, "embedding",
        lambda **kw: {"data": [{"embedding": [0.1, 0.2, 0.3]}]},
    )
    monkeypatch.setattr(
        litellm, "completion_cost", lambda completion_response=None: 0.001,
    )

    debits: list[tuple[float, str]] = []

    class _Wallet:
        def debit(self, amount_usdc, memo):
            debits.append((amount_usdc, memo))

    factories = _build_real_factories(
        _serve_args(
            "--episodic-memory", "lancedb",
            "--lancedb-path", str(tmp_path),
            "--litellm-url", "http://litellm:4000",
        )
    )
    mem = factories["episodic_memory_factory"](
        "darwin-1", wallet=_Wallet(), virtual_key="sk-darwin-1",
    )
    mem.record(tick=1, signature="sig", neuron="n", outcome="ok")

    assert len(debits) == 1
    amount, memo = debits[0]
    assert memo == "llm:embedding"
    assert amount == pytest.approx(0.001 * 1.10)  # provider cost + 10% markup


def test_episodic_embed_budget_refusal_fails_closed(tmp_path, monkeypatch):
    """A wallet refusal (out of funds / cap) must FAIL the embedding — a
    budget-less Darwin can't keep buying embeddings — but degrade the memory
    write instead of crashing the heartbeat (episodic record/recall aren't
    wrapped by the genome). So: no row stored, and no exception."""
    pytest.importorskip("lancedb")
    import litellm

    monkeypatch.setattr(
        litellm, "embedding",
        lambda **kw: {"data": [{"embedding": [0.4, 0.5, 0.6]}]},
    )
    monkeypatch.setattr(
        litellm, "completion_cost", lambda completion_response=None: 0.001,
    )

    class _BrokeWallet:
        def debit(self, amount_usdc, memo):
            raise RuntimeError("insufficient funds")

    factories = _build_real_factories(
        _serve_args(
            "--episodic-memory", "lancedb",
            "--lancedb-path", str(tmp_path),
            "--litellm-url", "http://litellm:4000",
        )
    )
    mem = factories["episodic_memory_factory"](
        "darwin-2", wallet=_BrokeWallet(), virtual_key="sk-darwin-2",
    )
    # Degrades: no crash, and the budget-failed write is NOT stored.
    mem.record(tick=1, signature="sig", neuron="n", outcome="ok")
    assert len(mem) == 0
    # Recall also degrades to empty rather than raising.
    assert mem.recall("sig") == []


def test_episodic_embed_proxy_429_degrades(tmp_path, monkeypatch):
    """A proxy budget-exhaustion 429 (production virtual-key enforcement)
    raises from litellm.embedding before any cost; the memory write degrades
    rather than crashing."""
    pytest.importorskip("lancedb")
    import litellm

    def _boom(**kw):
        raise RuntimeError("429 budget exceeded for key")

    monkeypatch.setattr(litellm, "embedding", _boom)

    debits: list = []

    class _Wallet:
        def debit(self, amount_usdc, memo):
            debits.append((amount_usdc, memo))

    factories = _build_real_factories(
        _serve_args(
            "--episodic-memory", "lancedb",
            "--lancedb-path", str(tmp_path),
            "--litellm-url", "http://litellm:4000",
        )
    )
    mem = factories["episodic_memory_factory"](
        "darwin-3", wallet=_Wallet(), virtual_key="sk-darwin-3",
    )
    mem.record(tick=1, signature="sig", neuron="n", outcome="ok")
    assert len(mem) == 0
    # No debit: the proxy refused before any cost was incurred.
    assert debits == []


def test_invoke_episodic_memory_factory_is_signature_aware():
    """The supervisor passes wallet/virtual_key only to factories that accept
    them; a legacy darwin_id-only factory still works."""
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        seen: dict = {}

        def metered(darwin_id, *, wallet=None, virtual_key=None):
            seen.update(darwin_id=darwin_id, wallet=wallet, virtual_key=virtual_key)
            return "metered-mem"

        sup._episodic_memory_factory = metered  # noqa: SLF001
        out = sup._invoke_episodic_memory_factory(  # noqa: SLF001
            "d-1", "the-wallet", "vk-1",
        )
        assert out == "metered-mem"
        assert seen == {
            "darwin_id": "d-1", "wallet": "the-wallet", "virtual_key": "vk-1",
        }

        # Legacy factory: only darwin_id — no crash, no extra kwargs.
        sup._episodic_memory_factory = lambda darwin_id: f"legacy:{darwin_id}"  # noqa: SLF001,E731
        assert (
            sup._invoke_episodic_memory_factory("d-2", "w", "vk")  # noqa: SLF001
            == "legacy:d-2"
        )
    finally:
        import asyncio

        asyncio.run(sup.shutdown())


# ---------------------------------------------------------------------------
# Drive-loop maintenance cadence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_price_oracle_injected_into_darwin_platform():
    """A price oracle handed to the supervisor reaches each Darwin's platform,
    so perceive marks holdings against live prices (doc 05 §Treasury)."""
    from darwin_platform.adapters.price_oracle import HttpPriceOracle

    oracle = HttpPriceOracle(quote_fn=lambda _id: 1.0)
    sup = Supervisor(heartbeat_seconds=0.0, price_oracle=oracle)
    try:
        status = await sup.spawn(opening_usdc=100.0, actor="test")
        ctx = sup._darwins[status.darwin_id].runtime._ctx  # noqa: SLF001
        assert ctx.price_oracle is oracle
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_maintenance_noop_without_services():
    """No drift detector, no merkle publisher → _run_maintenance is a
    safe no-op and the drive loop is unaffected."""
    sup = Supervisor(heartbeat_seconds=0.001, maintenance_interval_seconds=0.02)
    status = await sup.spawn(opening_usdc=100.0, actor="test")
    try:
        await asyncio.sleep(0.2)
        # The Darwin kept running; no maintenance services means no
        # maintenance audit entries.
        assert sup.audit_log.entries(action="merkle_anchor") == []
        st = await sup.status(status.darwin_id)
        assert st.alive is True
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_maintenance_runs_drift_and_merkle_on_cadence():
    detector = DriftDetector(min_observations=2)
    publisher = MerkleAnchorPublisher(backend=StubAnchorBackend())
    sup = Supervisor(
        heartbeat_seconds=0.001,
        maintenance_interval_seconds=0.05,
        drift_detector=detector,
        merkle_anchor_publisher=publisher,
    )
    status = await sup.spawn(opening_usdc=100.0, actor="test")
    did = status.darwin_id
    try:
        # ~10 maintenance intervals at 0.05s.
        await asyncio.sleep(0.6)

        # Drift profiling ran → the detector now holds rolling state for
        # this Darwin.
        assert detector.state_for(did) is not None
        state = detector.state_for(did)
        assert state.days_observed >= 1

        # Merkle anchoring ran → the publisher has anchors + the operator
        # audit log records each one.
        assert len(publisher.anchors()) >= 1
        merkle_audits = sup.audit_log.entries(action="merkle_anchor")
        assert len(merkle_audits) >= 1
        # The audit metadata carries the fleet root + darwin count.
        meta = merkle_audits[-1].metadata
        assert meta["darwin_count"] >= 1
        assert len(meta["fleet_root"]) == 64
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_maintenance_omnibus_reconciliation_audited():
    """With an omnibus wallet wired, the fleet maintenance pass records a
    reconciliation outcome on the operator audit log."""
    from tests.unit.darwin_platform.payments.test_omnibus_wallet import _Chain
    from darwin_platform.payments.omnibus_wallet import OmnibusWallet

    sup = Supervisor(
        heartbeat_seconds=0.001,
        maintenance_interval_seconds=0.05,
        omnibus_wallet=OmnibusWallet(_Chain(balance=0.0)),
    )
    await sup.spawn(opening_usdc=100.0, actor="test")
    try:
        await asyncio.sleep(0.4)
        # reconcile_omnibus runs in fleet maintenance; the books start
        # balanced (no deposits/funds yet) so the action is the clean one.
        reconciles = sup.audit_log.entries(action="omnibus_reconcile")
        discrepancies = sup.audit_log.entries(
            action="omnibus_reconcile_discrepancy"
        )
        assert len(reconciles) + len(discrepancies) >= 1
    finally:
        await sup.shutdown(kill_running=True)
