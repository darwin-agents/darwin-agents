"""HTTP route tests for round-4 wiring:
- POST /platform/signing/rotate
- GET /darwins/{id}/proposals
- POST /platform/darwins/{id}/proposals/{name}/admit
- POST /platform/darwins/{id}/proposals/{name}/reject
"""

from __future__ import annotations

import pytest

from darwin_dna.machinery.neuron_proposal import NeuronProposal


def _proposal(name="learned.summary"):
    return NeuronProposal(
        name=name,
        rationale="condense a search result into a 1-line takeaway",
        capability_tags=frozenset({"web.read"}),
        steps=("http GET https://api.search.example/summarize",),
        est_cost_usdc_p50=0.005,
        est_cost_usdc_p95=0.05,
    )


@pytest.mark.asyncio
async def test_rotate_signing_key_requires_persistence():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()  # no persistence
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.post("/platform/signing/rotate", json={})
        assert r.status_code == 409
        assert "persistence" in r.json()["detail"]
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_rotate_signing_key_succeeds_with_persistence(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor(persistence_path=str(tmp_path / "db.sqlite"))
    pem_before = sup.signing_public_key_pem()
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.post("/platform/signing/rotate", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["retired_public_key_pem"] == pem_before.decode("ascii")
    pem_after = sup.signing_public_key_pem()
    assert pem_after != pem_before
    await sup.shutdown(kill_running=True)
    sup._persistence.close()


@pytest.mark.asyncio
async def test_list_proposals_empty_for_new_darwin():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.get(f"/darwins/{status.darwin_id}/proposals")
        assert r.status_code == 200
        assert r.json() == {"pending": [], "admitted": []}
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_admit_proposal_via_http():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    did = status.darwin_id
    h = sup._darwins[did]
    p = _proposal()
    h.runtime.neuron_admission.submit(
        p, existing_catalog=h.runtime.catalog,
    )
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.post(
            f"/platform/darwins/{did}/proposals/{p.name}/admit"
        )
        assert r.status_code == 200
        # The proposal moved from pending → admitted; the listing reflects it.
        r2 = client.get(f"/darwins/{did}/proposals")
        admitted = r2.json()["admitted"]
        assert any(a["name"] == p.name for a in admitted)
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_admit_unknown_proposal_returns_409():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.post(
            f"/platform/darwins/{status.darwin_id}/proposals/learned.ghost/admit"
        )
        assert r.status_code == 409
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_reject_proposal_via_http():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    did = status.darwin_id
    h = sup._darwins[did]
    p = _proposal()
    h.runtime.neuron_admission.submit(
        p, existing_catalog=h.runtime.catalog,
    )
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.post(
            f"/platform/darwins/{did}/proposals/{p.name}/reject",
            json={"reason": "operator review failed"},
        )
        assert r.status_code == 200
        # No longer pending.
        r2 = client.get(f"/darwins/{did}/proposals")
        assert r2.json()["pending"] == []
    await sup.shutdown(kill_running=True)
