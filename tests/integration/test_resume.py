"""Survive sandbox replacement (docs/engineering/03, 12).

Working memory is wiped when the sandbox is replaced and reconstructed from
the last node-boundary checkpoint. The audit chain and wallet are durable
platform-side, so a resumed Darwin continues the *same* life — it does not
relive it (no second birth) and its audit chain stays unbroken across the seam.
"""

from __future__ import annotations

import asyncio

from darwin_dna.evolution import boot
from darwin_fakes import make_platform


def test_resume_from_checkpoint_continues_the_same_life(vclock):
    platform, stack = make_platform(opening_usdc=80.0, clock=vclock)

    rt1 = boot(platform, clock=vclock, sleep=vclock.sleep)
    assert rt1.resumed is False
    asyncio.run(rt1.run(max_heartbeats=7))
    tick_before = rt1.state.tick
    content_before = rt1.state.content_pieces
    assert tick_before == 7

    # --- sandbox replacement: a new process over the SAME durable platform
    # (same checkpoint store, audit sink, wallet, DNA → same darwin_id).
    rt2 = boot(stack.platform(), clock=vclock, sleep=vclock.sleep)

    assert rt2.resumed is True
    assert rt2.identity.darwin_id == rt1.identity.darwin_id
    assert rt2.state.tick == tick_before  # working memory reconstructed
    assert rt2.state.content_pieces == content_before
    rt2.audit.verify()  # chain unbroken across the replacement seam

    asyncio.run(rt2.run(max_heartbeats=5))
    assert rt2.state.tick == tick_before + 5  # continued, did not restart
    assert rt2.state.alive
    rt2.audit.verify()

    kinds = [r["kind"] for r in stack.audit_sink.read_all()]
    assert kinds.count("event:birth") == 1  # born once, ever
    assert kinds.count("event:resumed") >= 1


def test_genesis_boot_is_not_a_resume(vclock):
    platform, _ = make_platform(opening_usdc=50.0, clock=vclock)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    assert rt.resumed is False
    assert rt.state.tick == 0
