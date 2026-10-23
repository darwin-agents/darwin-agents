"""Genome hot paths (doc 02): the trust root, the kill switch, the treasury.

These run on every boot, every heartbeat, or before every side effect, so
each has a per-call budget and — where history accumulates — a scaling
check that the cost is independent of how long the Darwin has lived."""

from __future__ import annotations

import asyncio

from darwin_dna.machinery.kill_switch import KillSignal, KillSwitch
from darwin_dna.machinery.ledger import Ledger
from darwin_dna.manifest import verify_manifest
from darwin_dna.signature import load_platform_public_key
from darwin_fakes import make_platform
from darwin_fakes.fake_platform import KillFlag, SigningService, mint_genesis


def test_manifest_verification_per_boot(perf):
    signer = SigningService()
    manifest, blobs, _, _ = mint_genesis(signer, birth_ts=1_700_000_000.0)
    pub = load_platform_public_key(signer.public_key_pem())
    n = 200
    elapsed = perf.best_of(lambda: [verify_manifest(manifest, blobs, pub) for _ in range(n)])
    # ~0.2 ms on a laptop; budget 5 ms per verify.
    assert elapsed / n < perf.budget(0.005), f"{elapsed / n * 1e3:.2f} ms per verify"


def test_kill_switch_poll_is_cheap_and_final(perf):
    """``check()`` runs before every side effect, so it must cost
    microseconds; once tripped it must stay tripped on every later poll."""
    flag = KillFlag()
    ks = KillSwitch(flag)
    n = 50_000
    elapsed = perf.best_of(lambda: [ks.check() for _ in range(n)])
    assert elapsed / n < perf.budget(20e-6), f"{elapsed / n * 1e6:.1f} µs per poll"

    flag.flip("perf")
    for _ in range(1_000):
        try:
            ks.check()
        except KillSignal:
            continue
        raise AssertionError("a tripped kill switch let a poll through")


def test_kill_to_halt_latency_on_a_running_darwin(perf, vclock):
    """Doc 09: from flag-flip to halt within one heartbeat — the genome must
    not perform another neuron dispatch after the flip."""
    from darwin_dna.evolution import boot

    platform, stack = make_platform(opening_usdc=60.0, clock=vclock)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    asyncio.run(rt.run(max_heartbeats=5))
    stack.kill.flip("perf")
    before = rt.state.dispatches_done
    elapsed = perf.best_of(lambda: asyncio.run(rt.run(max_heartbeats=50)), repeat=1)
    assert rt.state.alive is False and rt.state.halt is True
    assert rt.state.dispatches_done == before, "work happened after the kill"
    assert elapsed < perf.budget(2.0), f"halt took {elapsed:.3f}s (doc 09 budget: < 2 s)"


def test_treasury_balance_is_constant_time(perf):
    """``treasury_balance()`` runs every heartbeat for reconcile; it is an
    O(1) running total, not a re-sum of the ledger (commit 9482e7f)."""

    def ledger_with(posts: int) -> Ledger:
        led = Ledger("perf", window=256)
        led.seed_opening(100.0, ts=0.0)
        led.journal_new(
            [{"credit": {"from": "rev", "amount_usdc": 1.0, "ts": float(i)}} for i in range(posts)],
            now=0.0,
        )
        return led

    short, long_ = ledger_with(100), ledger_with(20_000)
    n = 20_000
    t_short = perf.best_of(lambda: [short.treasury_balance() for _ in range(n)])
    t_long = perf.best_of(lambda: [long_.treasury_balance() for _ in range(n)])
    assert t_long < 2 * t_short + 0.002, f"{t_short * 1e3:.2f} ms → {t_long * 1e3:.2f} ms"
    # The window bounds RAM while the balance stays exact over all posts.
    assert len(long_.entries) == 256
    assert long_.treasury_balance() == 100.0 + 20_000


def test_heartbeat_throughput_on_fakes(perf, vclock):
    """A whole heartbeat (perceive → plan → dispatch → reflect) against the
    fake stack. Bounds the per-beat overhead the genome adds on top of real
    I/O, which is what the supervisor multiplies by the fleet size."""
    from darwin_dna.evolution import boot

    platform, _ = make_platform(opening_usdc=60.0, clock=vclock)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    beats = 200
    elapsed = perf.best_of(lambda: asyncio.run(rt.run(max_heartbeats=beats)), repeat=1)
    assert rt.state.alive, rt.state.death_reason
    assert elapsed / beats < perf.budget(0.05), f"{elapsed / beats * 1e3:.1f} ms per heartbeat"
