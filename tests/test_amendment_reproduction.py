"""Forced reproduction with amendment layer (docs/engineering/02, doc 09).

Doc 02: "For critical rule updates the platform can force a Darwin to
undergo a reproduction event using a platform-supplied amendment layer.
The child inherits the amendment; the parent dies."

These tests:
1. The genome's ``reproduce()`` accepts an amendment and the resulting
   child manifest verifies through the trust root with the new layer
   between the inherited stack and the freshly distilled lessons.
2. The runtime's one-shot amendment slot survives reproduction (cleared
   after consumption; not stuck for the next event).
3. The Supervisor's ``force_reproduce_with_amendment`` schedules the
   reproduction AND kills the parent once the child appears.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Callable

import pytest

from darwin_dna.layers import LAYER_0_LESSONS, serialize_lessons
from darwin_dna.manifest import Manifest
from darwin_platform import Supervisor, SupervisorError


async def _wait_until(
    pred: Callable[[], bool], *, timeout: float = 5.0, tick: float = 0.01
) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(tick)


def _amendment_blob() -> bytes:
    # Smallest valid lessons-shape payload: an empty list. A real amendment
    # would carry a structured constitutional update; the trust root is
    # blob-agnostic, signed by the platform key.
    return serialize_lessons(LAYER_0_LESSONS)


# -- 1. The genome accepts the amendment + the child manifest verifies ---
async def test_runtime_install_amendment_then_reproduce(supervisor=None) -> None:
    sup = supervisor or Supervisor(heartbeat_seconds=0.0)
    try:
        status = await sup.spawn(opening_usdc=200.0)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        # Let the genome run long enough for perceive() to set the profit
        # baseline so reproduce() computes a positive child budget.
        await _wait_until(lambda: h.runtime.state.profit_baseline_usdc > 0)

        # Stage an amendment and request reproduction.
        h.runtime.install_amendment(_amendment_blob(), "layer_amendment_test")
        assert h.runtime.has_pending_amendment()
        h.reproduce_requested = True

        await _wait_until(lambda: len(h.children) >= 1, timeout=10.0)
        child_id = h.children[0]
        assert child_id in sup._darwins  # noqa: SLF001

        # The amendment slot is one-shot: cleared after the genome consumed
        # it. A follow-up reproduction must not silently re-apply.
        assert not h.runtime.has_pending_amendment()

        # The child's manifest carries the amendment layer between the
        # inherited stack (layer 0) and the freshly distilled lessons
        # (layer 2 in the child).
        spawn_record = h.stack.spawner.children[0]
        manifest = Manifest.model_validate_json(spawn_record["child_manifest"])
        names = [e.name for e in manifest.layers]
        assert "layer_amendment_test" in names
        # Order: inherited layer first, amendment in the middle (since the
        # lesson distiller may produce 0 new lessons here), then the
        # generation-1 lessons layer last.
        assert names[0] == "layer_0_lessons"
        # Last entry is the new-lessons layer for generation 1.
        assert names[-1] == "layer_1_lessons"
        # Amendment sits between.
        assert "layer_amendment_test" in names[1:-1]
    finally:
        await sup.shutdown()


# -- 2. Supervisor force_reproduce_with_amendment + parent dies ----------
async def test_supervisor_force_amendment_reproduction_kills_parent() -> None:
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        status = await sup.spawn(opening_usdc=200.0)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        await _wait_until(lambda: h.runtime.state.profit_baseline_usdc > 0)

        await sup.force_reproduce_with_amendment(
            status.darwin_id,
            amendment_blob=_amendment_blob(),
            amendment_name="layer_amendment_kyc_required",
            kill_parent_reason="testing amendment kill",
        )

        # Child appears.
        await _wait_until(lambda: len(h.children) >= 1, timeout=10.0)
        # Parent's kill flag is flipped on the same drive cycle as the
        # child observation; the genome then halts cleanly.
        await _wait_until(lambda: not h.runtime.state.alive, timeout=5.0)
        final = await sup.status(status.darwin_id)
        assert final.killed
        assert "testing amendment kill" in (final.death_reason or "")

        # Parent's audit chain holds the kill.halt + reproduction.done.
        events = await sup.events(status.darwin_id, limit=10_000)
        kinds = [e.get("kind") for e in events["events"]]
        assert "event:kill.halt" in kinds
        assert "event:reproduction.done" in kinds
    finally:
        await sup.shutdown()


async def test_amendment_request_rejects_killed_parent() -> None:
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        status = await sup.spawn(opening_usdc=200.0)
        await sup.kill(status.darwin_id)
        with pytest.raises(SupervisorError, match="killed"):
            await sup.force_reproduce_with_amendment(
                status.darwin_id,
                amendment_blob=_amendment_blob(),
                amendment_name="x",
            )
    finally:
        await sup.shutdown()


async def test_install_amendment_validates_args() -> None:
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        status = await sup.spawn(opening_usdc=60.0)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        with pytest.raises(TypeError):
            h.runtime.install_amendment("not bytes", "x")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            h.runtime.install_amendment(b"\x00", "")  # empty name
    finally:
        await sup.shutdown()


# -- 3. HTTP endpoint -----------------------------------------------------
async def test_amendment_endpoint() -> None:
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from darwin_platform.api import build_app  # type: ignore

    sup = Supervisor(heartbeat_seconds=0.0)
    app = build_app(sup)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            r = await c.post("/darwins", json={"opening_usdc": 200.0})
            did = r.json()["darwin_id"]
            h = sup._darwins[did]  # noqa: SLF001
            await _wait_until(lambda: h.runtime.state.profit_baseline_usdc > 0)

            payload = {
                "amendment_blob_b64": base64.b64encode(_amendment_blob()).decode(),
                "amendment_name": "layer_amendment_http",
                "kill_parent_reason": "http test",
            }
            r = await c.post(
                f"/platform/darwins/{did}/reproduce_with_amendment", json=payload
            )
            assert r.status_code == 200, r.text

            # Eventually a child appears + parent dies.
            await _wait_until(lambda: len(h.children) >= 1, timeout=10.0)
            await _wait_until(lambda: not h.runtime.state.alive, timeout=5.0)

            # Bad base64 → 400.
            r = await c.post(
                f"/platform/darwins/{did}/reproduce_with_amendment",
                json={
                    "amendment_blob_b64": "not!base64",
                    "amendment_name": "x",
                },
            )
            assert r.status_code in (400, 409)  # 400 for bad b64; 409 if killed

            # Unknown id → 404.
            r = await c.post(
                "/platform/darwins/nope/reproduce_with_amendment",
                json={
                    "amendment_blob_b64": base64.b64encode(b"x").decode(),
                    "amendment_name": "x",
                },
            )
            assert r.status_code == 404
        finally:
            await sup.shutdown()
