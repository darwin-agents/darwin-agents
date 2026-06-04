"""End-to-end Phase 1 integration tests (docs/engineering/21).

Drives the supervisor through the patch-staging path + lineage-tree
endpoint + Phase 1 metrics to confirm the full wiring works together.

These are integration-tier in spirit (multiple modules interacting)
but pure-Python (no docker / no real chain): everything runs through
the in-process supervisor with the in-process staging fallback.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

from darwin_platform.evolution.eval_runner import EvalRunner
from darwin_platform.evolution.patch_staging import (
    PatchStagingService,
)
from darwin_platform.runtime.supervisor import Supervisor, SupervisorError


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


async def test_no_soma_merge_hook_without_soma_root():
    """Without a configured soma root the merge hook is absent — the genome
    keeps the Phase-0 simulation (#16)."""
    sup = Supervisor(heartbeat_seconds=0.001)
    status = await sup.spawn(opening_usdc=1000.0, actor="test")
    try:
        ctx = sup._darwins[status.darwin_id].runtime._ctx  # noqa: SLF001
        assert getattr(ctx, "soma_merge", None) is None
        assert sup.soma_merger_for(status.darwin_id) is None
    finally:
        await sup.shutdown(kill_running=True)


async def test_supervisor_pins_per_darwin_soma_merge_hook(tmp_path):
    """With a soma root, spawn installs a per-Darwin merger rooted under
    ``<soma_root>/<darwin_id>`` on the genome's ctx (#16)."""
    sup = Supervisor(heartbeat_seconds=0.001, soma_root=tmp_path)
    status = await sup.spawn(opening_usdc=1000.0, actor="test")
    try:
        ctx = sup._darwins[status.darwin_id].runtime._ctx  # noqa: SLF001
        assert callable(ctx.soma_merge.merge)
        merger = sup.soma_merger_for(status.darwin_id)
        assert merger is not None
        # Rooted per-Darwin so one tenant can't touch another's soma.
        assert merger.root == (tmp_path / status.darwin_id).resolve()
    finally:
        await sup.shutdown(kill_running=True)


async def test_kill_captures_death_forensic_soma_snapshot(tmp_path):
    """On kill, the supervisor snapshots the whole soma tree for post-mortem
    forensics (doc 05 death sequence) — distinct from soma_merge's in-memory
    mutation-time rollback snapshots, which die with the organism."""
    import json

    sup = Supervisor(heartbeat_seconds=0.001, soma_root=tmp_path)
    status = await sup.spawn(opening_usdc=1000.0, actor="test")
    did = status.darwin_id
    merger = sup.soma_merger_for(did)
    # Seed a file in the live soma so the snapshot has something to capture.
    (merger.root / "soma" / "strategies").mkdir(parents=True, exist_ok=True)
    (merger.root / "soma" / "strategies" / "live.py").write_text(
        "ALPHA = 1\n"
    )
    try:
        await sup.kill(did, reason="owner kill", actor="alice")

        handle = sup._darwins[did]  # noqa: SLF001
        assert handle.forensics_captured is True

        # The capture is on the operator audit chain.
        rows = sup._audit.entries(  # noqa: SLF001
            action="soma_forensic_snapshot", darwin_id=did,
        )
        assert len(rows) == 1
        meta = rows[0].metadata
        assert meta["captured"] is True
        assert meta["reason"] == "owner kill"
        assert meta["file_count"] >= 1

        # The persisted manifest carries the live soma byte-for-byte and
        # records the death reason in its metadata.
        manifest = json.loads(Path(meta["path"]).read_text())
        assert (
            manifest["contents"]["soma/strategies/live.py"] == "ALPHA = 1\n"
        )
        assert manifest["reason"] == "owner kill"
        assert manifest["metadata"]["killed"] is True
    finally:
        await sup.shutdown(kill_running=True)


async def test_natural_death_drive_loop_captures_forensic_snapshot(tmp_path):
    """Natural (bankruptcy) death exits the drive loop, which captures the
    forensic soma snapshot on the way out — same helper as the kill path, a
    different call site (doc 05)."""
    import json

    sup = Supervisor(heartbeat_seconds=0.001, soma_root=tmp_path)
    status = await sup.spawn(opening_usdc=1000.0, actor="test")
    did = status.darwin_id
    merger = sup.soma_merger_for(did)
    (merger.root / "soma").mkdir(parents=True, exist_ok=True)
    (merger.root / "soma" / "s.py").write_text("X = 1\n")
    handle = sup._darwins[did]  # noqa: SLF001
    try:
        # Stand in for the genome's bankruptcy verdict: once not-alive, the
        # drive loop's `while runtime.state.alive` exits and runs the capture.
        handle.runtime.state.death_reason = "bankruptcy: test"
        handle.runtime.state.alive = False
        for _ in range(200):
            if handle.forensics_captured:
                break
            await asyncio.sleep(0.005)
        assert handle.forensics_captured is True

        rows = sup._audit.entries(  # noqa: SLF001
            action="soma_forensic_snapshot", darwin_id=did,
        )
        assert len(rows) == 1
        assert rows[0].metadata["reason"] == "bankruptcy: test"
        manifest = json.loads(Path(rows[0].metadata["path"]).read_text())
        assert manifest["contents"]["soma/s.py"] == "X = 1\n"
    finally:
        await sup.shutdown(kill_running=False)


async def test_supervisor_lists_and_reads_death_forensics(tmp_path):
    """The supervisor surfaces a dead Darwin's death snapshots for operators
    (the retrieval seam behind the HTTP forensics routes)."""
    sup = Supervisor(heartbeat_seconds=0.001, soma_root=tmp_path)
    status = await sup.spawn(opening_usdc=1000.0, actor="test")
    did = status.darwin_id
    merger = sup.soma_merger_for(did)
    (merger.root / "soma").mkdir(parents=True, exist_ok=True)
    (merger.root / "soma" / "live.py").write_text("ALPHA = 1\n")
    try:
        # Unknown / never-captured Darwin is queryable, not an error.
        assert sup.list_soma_forensics("darwin-nope") == []

        await sup.kill(did, reason="owner kill", actor="alice")

        rows = sup.list_soma_forensics(did)
        assert len(rows) == 1
        sid = rows[0]["snapshot_id"]
        assert rows[0]["reason"] == "owner kill"
        assert "contents" not in rows[0]  # index is metadata-only

        full = sup.read_soma_forensic(did, sid)
        assert full["contents"]["soma/live.py"] == "ALPHA = 1\n"

        with pytest.raises(SupervisorError):
            sup.read_soma_forensic(did, "death-1")
    finally:
        await sup.shutdown(kill_running=True)


async def test_forensics_http_routes(tmp_path):
    """GET /platform/darwins/{id}/forensics[/{snapshot_id}] expose the death
    snapshots over HTTP for the operator post-mortem surface."""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from darwin_platform.api import build_app

    sup = Supervisor(heartbeat_seconds=0.001, soma_root=tmp_path)
    status = await sup.spawn(opening_usdc=1000.0, actor="test")
    did = status.darwin_id
    merger = sup.soma_merger_for(did)
    (merger.root / "soma").mkdir(parents=True, exist_ok=True)
    (merger.root / "soma" / "live.py").write_text("BETA = 2\n")
    await sup.kill(did, reason="owner kill", actor="alice")

    app = build_app(sup)
    try:
        with TestClient(app) as client:
            r = client.get(f"/platform/darwins/{did}/forensics")
            assert r.status_code == 200
            snaps = r.json()["snapshots"]
            assert len(snaps) == 1
            sid = snaps[0]["snapshot_id"]

            r2 = client.get(f"/platform/darwins/{did}/forensics/{sid}")
            assert r2.status_code == 200
            assert r2.json()["contents"]["soma/live.py"] == "BETA = 2\n"

            r3 = client.get(f"/platform/darwins/{did}/forensics/death-1")
            assert r3.status_code == 404
    finally:
        await sup.shutdown(kill_running=False)


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
    from darwin_platform.runtime.supervisor import LineageEdge

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
    from darwin_platform.runtime.supervisor import UnknownDarwin

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
    from darwin_platform.runtime.supervisor import _GenomeStagingHook  # noqa: SLF001
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
    from darwin_platform.security.signing_service import LocalEd25519Signer

    custom = LocalEd25519Signer()
    sup = Supervisor(heartbeat_seconds=0.001, signer=custom)
    try:
        assert sup._signer is custom  # noqa: SLF001
    finally:
        await sup.shutdown(kill_running=True)
