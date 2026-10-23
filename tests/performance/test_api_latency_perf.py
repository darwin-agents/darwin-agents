"""Control-plane API latency (doc 08) with a populated fleet.

In-process via ``httpx.ASGITransport`` so the numbers are the platform's own
overhead — routing, auth, supervisor reads, serialisation — not the
network. The dashboard polls these routes continuously, so each has a p95
budget, and a single Darwin's status must not degrade with fleet size.

The fleet runs at a fast-but-real cadence for a moment to build history,
then is paused so the measurement isn't queueing behind live drive loops.

ponytail: in the in-process driver, events / anatomy / metrics cost
O(audit-chain length) (the in-memory sink copies the chain per read, ~6 ms
per 1k records). Production uses ``--driver k8s``, where the supervisor
reads a bounded pod tail (``EVENTS_TAIL``); index the in-process reads if
the dev path ever has to serve long-lived Darwins."""

from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from darwin_platform import Supervisor  # noqa: E402
from darwin_platform.api import build_app  # noqa: E402

FLEET = 25


async def _p95(client, path: str, n: int = 60) -> float:
    samples = []
    for _ in range(n):
        t = time.perf_counter()
        r = await client.get(path)
        samples.append(time.perf_counter() - t)
        assert r.status_code == 200, (path, r.text)
    samples.sort()
    return samples[int(0.95 * (len(samples) - 1))]


async def _fleet(n: int, *, warm_seconds: float = 0.5):
    """``n`` Darwins with ~``warm_seconds`` of history each, then paused."""
    sup = Supervisor(heartbeat_seconds=0.01)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(sup)), base_url="http://t")
    spawn_times, ids = [], []
    for _ in range(n):
        t = time.perf_counter()
        r = await client.post("/darwins", json={"opening_usdc": 60.0})
        spawn_times.append(time.perf_counter() - t)
        ids.append(r.json()["darwin_id"])
    await asyncio.sleep(warm_seconds)
    assert (await client.post("/platform/fleet/pause", json={"reason": "perf"})).status_code == 200
    await asyncio.sleep(0.1)  # let every loop observe the pause
    return sup, client, ids, spawn_times


def test_read_routes_meet_their_p95_budgets(perf):
    async def scenario():
        sup, client, ids, spawn_times = await _fleet(FLEET)
        try:
            did = ids[0]
            budgets = {
                "/healthz": 0.01,
                f"/darwins/{did}": 0.05,
                f"/darwins/{did}/events?limit=100": 0.10,
                f"/darwins/{did}/ledger?limit=100": 0.10,
                f"/darwins/{did}/anatomy": 0.10,
                "/darwins": 0.05,
                "/metrics": 0.25,
            }
            results = {path: await _p95(client, path) for path in budgets}
            return results, budgets, sorted(spawn_times)[len(spawn_times) // 2]
        finally:
            await client.aclose()
            await sup.shutdown()

    results, budgets, spawn_median = asyncio.run(scenario())
    over = {
        p: f"{results[p] * 1e3:.1f} ms > {perf.budget(b) * 1e3:.0f} ms"
        for p, b in budgets.items() if results[p] > perf.budget(b)
    }
    assert not over, f"p95 over budget with a {FLEET}-Darwin fleet: {over}"
    assert spawn_median < perf.budget(0.5), f"median spawn {spawn_median * 1e3:.0f} ms"


def test_status_latency_does_not_scale_with_fleet_size(perf):
    """Reading one Darwin's status must not walk the whole fleet."""

    async def measure(n: int) -> float:
        sup, client, ids, _ = await _fleet(n)
        try:
            return await _p95(client, f"/darwins/{ids[0]}", n=40)
        finally:
            await client.aclose()
            await sup.shutdown()

    small = asyncio.run(measure(2))
    large = asyncio.run(measure(20))
    assert large < 3 * small + 0.005, f"status p95 {small * 1e3:.2f} ms → {large * 1e3:.2f} ms"
