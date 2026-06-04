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
