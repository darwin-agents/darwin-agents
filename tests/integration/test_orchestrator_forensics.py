"""Pod-side death-time forensics in k8s mode (doc 05).

A k8s pod has no mutable soma tree (soma-merge is in-process only), so its
death-time "what code was running" record is the SIGNED layer manifest — each
layer's SHA-256 plus the genome identity. The orchestrator captures this on
death and exposes it on the control API so the supervisor can pull + persist it
before reaping the pod.
"""

from __future__ import annotations

import pytest

from darwin_dna.evolution import boot
from darwin_fakes import make_platform
from darwin_platform.runtime.orchestrator import (
    OrchestratorState,
    _build_control_app,
)


def _state(vclock, darwin_id="pod-x"):
    platform, stack = make_platform(opening_usdc=80.0, clock=vclock)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    return OrchestratorState(
        darwin_id=darwin_id, stack=stack, runtime=rt, clock=vclock,
    )


def test_forensics_captures_signed_layer_fingerprint(vclock):
    state = _state(vclock)
    f = state.forensics()
    assert f["schema"] == "darwin-platform.soma-forensic-k8s/1"
    assert f["snapshot_id"].startswith("death-")
    # The signed layers are the soma in k8s mode.
    assert f["file_count"] == len(f["layers"]) > 0
    assert all({"index", "name", "sha256"} <= set(l) for l in f["layers"])
    assert f["identity"]["darwin_id"]
    assert f["soma_digest"]


def test_install_pod_soma_merge_pins_hook(vclock, tmp_path):
    from darwin_platform.evolution.soma_merge import SomaMerger
    from darwin_platform.runtime.orchestrator import _install_pod_soma_merge

    platform, stack = make_platform(opening_usdc=80.0, clock=vclock)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    merger = _install_pod_soma_merge(rt, tmp_path, "pod-x", vclock)
    assert isinstance(merger, SomaMerger)
    # apply_mutation reaches it through ctx.soma_merge (real self-mod, not sim).
    assert rt._ctx.soma_merge is merger  # noqa: SLF001
    # No soma root → no hook (genome keeps the Phase-0 simulation).
    assert _install_pod_soma_merge(rt, None, "pod-x", vclock) is None


def test_forensics_includes_real_soma_when_merger_wired(vclock, tmp_path):
    from darwin_platform.evolution.soma_merge import SomaMerger

    merger = SomaMerger(tmp_path / "pod-x", clock=vclock)
    (merger.root / "soma" / "strategies").mkdir(parents=True)
    (merger.root / "soma" / "strategies" / "x.py").write_text("GAIN = 2\n")
    platform, stack = make_platform(opening_usdc=80.0, clock=vclock)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    state = OrchestratorState(
        darwin_id="pod-x", stack=stack, runtime=rt, clock=vclock,
        soma_merger=merger,
    )
    f = state.forensics()
    # Real soma file contents captured — not just the layer fingerprint.
    assert f["metadata"]["soma_present"] is True
    assert f["contents"]["soma/strategies/x.py"] == "GAIN = 2\n"
    assert f["file_count"] == 1
    assert f["files"]["soma/strategies/x.py"]["bytes"] == len(b"GAIN = 2\n")
    # The signed layer fingerprint is still carried alongside.
    assert f["layers"] and f["layer_digest"]


_POD_STRATEGY = '''
from ._base import StrategyAction, StrategySpec


class EvolvedStrategy:
    _spec = StrategySpec(
        id="evolved_pod", archetype="content.affiliate",
        neurons_used=frozenset(), required_capabilities=frozenset(),
        fitness_signals=["x"], est_daily_cost_usdc=0.1,
        revenue_cadence_days=7.0,
    )

    @property
    def spec(self):
        return self._spec

    def propose(self, obs):
        return StrategyAction(kind="wait", reason="evolved")


def founding_strategy():
    return EvolvedStrategy()
'''


def test_pod_loads_soma_strategy_before_boot(vclock, tmp_path):
    """The pod activates its evolved soma (doc 33): _load_pod_soma_strategy
    injects the overlay on the platform so boot binds it. No-op + fail-closed
    without a soma root."""
    from darwin_platform.runtime.orchestrator import _load_pod_soma_strategy

    sdir = tmp_path / "pod-x" / "soma" / "strategies"
    sdir.mkdir(parents=True)
    (sdir / "content_affiliate.py").write_text(_POD_STRATEGY)

    platform, _ = make_platform(opening_usdc=80.0, clock=vclock)
    _load_pod_soma_strategy(platform, tmp_path, "pod-x")
    assert getattr(platform, "strategy", None) is not None
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    assert rt.strategy.spec.id == "evolved_pod"

    # No soma root → no injection (founding stays).
    p2, _ = make_platform(opening_usdc=80.0, clock=vclock)
    _load_pod_soma_strategy(p2, None, "pod-x")
    assert getattr(p2, "strategy", None) is None


def test_forensics_marks_soma_absent_without_merger(vclock):
    # Back-compat: no merger → layer-fingerprint forensic, soma_present False.
    state = _state(vclock)
    f = state.forensics()
    assert f["metadata"]["soma_present"] is False
    assert "contents" not in f
    assert f["file_count"] == len(f["layers"]) > 0
    assert f["soma_digest"] == f["layer_digest"]


def test_forensics_freezes_reason_at_death(vclock):
    state = _state(vclock)
    state.runtime.state.alive = False
    state.runtime.state.death_reason = "bankruptcy: broke"
    state._capture_death_forensics()
    f = state.forensics()
    assert f["reason"] == "bankruptcy: broke"
    assert f["metadata"]["death_reason"] == "bankruptcy: broke"
    # Idempotent: a second capture doesn't overwrite the frozen record.
    sid = f["snapshot_id"]
    state._capture_death_forensics()
    assert state.forensics()["snapshot_id"] == sid


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_forensics_route_requires_token(vclock):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    state = _state(vclock)
    app = _build_control_app(state, "tok")
    with TestClient(app) as client:
        assert client.get("/forensics").status_code == 401
        r = client.get(
            "/forensics", headers={"Authorization": "Bearer tok"}
        )
        assert r.status_code == 200
        assert r.json()["schema"] == "darwin-platform.soma-forensic-k8s/1"
