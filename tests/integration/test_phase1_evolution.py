"""End-to-end Phase 1 integration tests (docs/engineering/21).

Drives the supervisor through the patch-staging path + lineage-tree
endpoint + Phase 1 metrics to confirm the full wiring works together.

These are integration-tier in spirit (multiple modules interacting)
but pure-Python (no docker / no real chain): everything runs through
the in-process supervisor with the in-process staging fallback.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from darwin_dna.evolution import boot
from darwin_dna.machinery.eval_harness import BehaviorDelta
from darwin_dna.machinery.patch_validator import FileChange, Patch
from darwin_platform.eval_runner import EvalRequest, EvalRunner
from darwin_platform.patch_staging import (
    PatchStagingService,
    StagingQuota,
)
from darwin_platform.supervisor import Supervisor


@pytest.mark.asyncio
async def test_supervisor_pins_patch_staging_hook_on_ctx():
    """After spawn, the genome's ctx carries ``patch_staging`` so the
    evaluate node can dispatch into the platform service."""
    sup = Supervisor(heartbeat_seconds=0.001)
    status = await sup.spawn(opening_usdc=1000.0, actor="test")
    try:
        handle = sup._darwins[status.darwin_id]  # noqa: SLF001
        ctx = handle.runtime._ctx  # noqa: SLF001
        assert hasattr(ctx, "patch_staging")
        # The hook is a callable wrapper, not the raw service.
        assert callable(ctx.patch_staging.evaluate)
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_lineage_tree_endpoint_shape():
    sup = Supervisor(heartbeat_seconds=0.001)
    status = await sup.spawn(opening_usdc=1000.0, actor="test")
    try:
        tree = await sup.lineage_tree(status.darwin_id)
        assert tree["darwin_id"] == status.darwin_id
        assert tree["generation"] == 0
        assert tree["ancestors"] == []
        assert tree["descendants"] == []
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_lineage_tree_with_synthetic_descendant():
    """Add a lineage edge directly so the tree query returns it (the
    full reproduction event is exercised in test_self_modification)."""
    from darwin_platform.supervisor import LineageEdge

    sup = Supervisor(heartbeat_seconds=0.001)
    status = await sup.spawn(opening_usdc=1000.0, actor="test")
    try:
        sup._lineage_registry.add(LineageEdge(  # noqa: SLF001
            parent_id=status.darwin_id, child_id="child-1",
            spawned_at=1234.5, budget_usdc=50.0,
        ))
        sup._lineage_registry.add(LineageEdge(  # noqa: SLF001
            parent_id="child-1", child_id="grandchild-1",
            spawned_at=1500.0, budget_usdc=25.0,
        ))
        tree = await sup.lineage_tree(status.darwin_id)
        assert len(tree["descendants"]) == 1
        child = tree["descendants"][0]
        assert child["darwin_id"] == "child-1"
        assert child["budget_usdc"] == 50.0
        assert child["alive"] is False  # not actually booted
        assert len(child["children"]) == 1
        assert child["children"][0]["darwin_id"] == "grandchild-1"
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_lineage_tree_unknown_darwin_raises():
    from darwin_platform.supervisor import UnknownDarwin

    sup = Supervisor(heartbeat_seconds=0.001)
    try:
        with pytest.raises(UnknownDarwin):
            await sup.lineage_tree("does-not-exist")
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_patch_staging_status_surface():
    """The platform-side staging status reports quota + history."""
    sup = Supervisor(heartbeat_seconds=0.001)
    try:
        status = sup.patch_staging.status()
        assert "quota" in status
        assert status["quota"]["max_concurrent"] >= 1
        assert status["history_size"] == 0
        assert status["recent"] == []
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_eval_runner_uses_per_darwin_audit_sink():
    """Confirms the supervisor wires the audit-sink resolver so the
    runner reads through the same sink the genome wrote to."""
    sup = Supervisor(heartbeat_seconds=0.001)
    status = await sup.spawn(opening_usdc=1000.0, actor="test")
    try:
        # Run one heartbeat so the boot/heartbeat audit entries land.
        handle = sup._darwins[status.darwin_id]  # noqa: SLF001
        await handle.runtime.run(max_heartbeats=1)
        # Resolver should hit the same sink.
        sink = sup._resolve_audit_sink_for(status.darwin_id)  # noqa: SLF001
        assert sink is not None
        rows = sink.read_all()
        assert len(rows) > 0
        assert all(r.get("darwin_id") == status.darwin_id for r in rows)
        # Unknown id ⇒ None (resolver miss is the empty-audit fallback path).
        assert sup._resolve_audit_sink_for("nope") is None  # noqa: SLF001
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_evaluate_mutation_via_staging_records_via_in_audit():
    """An end-to-end exercise of the staging path: feed a known
    promotable BehaviorDelta + valid patch through the evaluate node
    with the staging hook installed and confirm the audit records that
    staging produced the verdict (via=staging)."""
    from darwin_dna.machinery.audit_logger import AuditLogger
    from darwin_dna.orchestrator_skeleton.nodes import evaluate_mutation
    from darwin_dna.orchestrator_skeleton.state import OrchestratorState
    from darwin_dna.hyperparameters import DEFAULT_HYPERPARAMETERS
    from darwin_fakes.fake_platform import MemoryAuditSink

    sink = MemoryAuditSink()
    audit = AuditLogger("d-1", sink)

    staging = PatchStagingService(sandbox_factory=None)
    runner = EvalRunner(
        staging=staging,
        audit_sink_resolver=lambda _id: sink,
    )
    # The hook the supervisor would install:
    from darwin_platform.supervisor import _GenomeStagingHook  # noqa: SLF001
    hook = _GenomeStagingHook(runner=runner, darwin_id="d-1")

    ctx = types.SimpleNamespace(
        audit=audit, hp=DEFAULT_HYPERPARAMETERS,
        patch_staging=hook,
    )
    state = OrchestratorState()
    state.plan = {
        "failure_class": "flaky_io", "recurrence": 10,
        "patch": {
            "rationale": "back off",
            "changes": [{
                "path": "soma/strategies/x.py", "diff": "+    pass\n",
            }],
        },
        "behavior_delta": {
            "kind": "backoff", "failure_class": "flaky_io",
            "after_failures": 2,
        },
    }
    evaluate_mutation.run(state, ctx)

    assert state.route == "apply_mutation"
    # The outcome for the evaluate intent must carry ``via='staging'``.
    rows = sink.read_all()
    outcomes = [
        r for r in rows
        if r.get("kind") == "outcome"
        and (r.get("payload", {}) or {}).get("detail", {}).get("via")
    ]
    assert outcomes, "no evaluate outcome with via marker"
    assert outcomes[-1]["payload"]["detail"]["via"] == "staging"


@pytest.mark.asyncio
async def test_publishing_endpoints_wired_onto_neuron():
    """Phase 1: supervisor pins real endpoints on content.publisher post-boot."""
    sup = Supervisor(
        heartbeat_seconds=0.001,
        publishing_endpoints={
            "wordpress": "https://real-wp.example/posts",
            "medium": "https://real-medium.example/v1/posts",
        },
        analytics_endpoint="https://real-analytics.example/metrics",
    )
    status = await sup.spawn(opening_usdc=1000.0, actor="test")
    try:
        handle = sup._darwins[status.darwin_id]  # noqa: SLF001
        publisher = handle.runtime.catalog.get("content.publisher")
        assert publisher is not None
        assert publisher.endpoint_overrides["wordpress"] == (
            "https://real-wp.example/posts"
        )
        analytics = handle.runtime.catalog.get("analytics.measurer")
        assert analytics is not None
        assert analytics.default_endpoint == (
            "https://real-analytics.example/metrics"
        )
    finally:
        await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_signer_injection_takes_precedence():
    """An injected signer (Phase 1 HSM path) bypasses the persistence
    seed + the FakeSigningService default."""
    from darwin_platform.signing_service import LocalEd25519Signer

    custom = LocalEd25519Signer()
    sup = Supervisor(heartbeat_seconds=0.001, signer=custom)
    try:
        assert sup._signer is custom  # noqa: SLF001
    finally:
        await sup.shutdown(kill_running=True)
