"""Multi-generation reproduction (docs/engineering/06).

The strongest reproduction test: don't just trust that a child was recorded —
seal it, then actually BOOT the child's DNA through the exact trust root a
real spawn would, and run a generation-1 organism.
"""

from __future__ import annotations

import asyncio

import pytest

from darwin_dna.evolution import boot
from darwin_fakes import make_child_platform, make_platform


def _force_reproduction(rt, stack, *, balance: float):
    rt.state.balance_usdc = balance
    stack.wallet._balance = balance
    rt.state.profit_baseline_usdc = 50.0
    rt.state.reproduce = True
    rt._reproduce()
    return stack.spawner.children[-1]


def test_reproduce_then_boot_and_run_the_child(vclock):
    platform, stack = make_platform(opening_usdc=200.0, clock=vclock)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    asyncio.run(rt.run(max_heartbeats=20))

    rec = _force_reproduction(rt, stack, balance=400.0)

    # Budget is a fraction of surplus above minimum-viable.
    surplus = 400.0 - rt._hp.minimum_viable_balance_usdc
    assert abs(rec["budget_usdc"] - surplus * rt._hp.profit_transfer_fraction) < 1e-6

    # Boot the child from its sealed DNA — verifies the full trust root.
    cplat, cstk = make_child_platform(
        signer=stack.signer,
        child_manifest_json=rec["child_manifest"],
        child_layer_blobs=rec["child_layer_blobs"],
        opening_usdc=rec["budget_usdc"],
        clock=vclock,
    )
    child = boot(cplat, clock=vclock, sleep=vclock.sleep)

    assert child.identity.generation == 1
    assert child.identity.parent_id == rt.identity.darwin_id
    assert child.identity.lineage == [rt.identity.darwin_id]
    assert child.identity.darwin_id != rt.identity.darwin_id

    st = asyncio.run(child.run(max_heartbeats=15))
    assert st.alive
    assert st.content_pieces == 8
    child.audit.verify()  # the child keeps its own unbroken chain


def test_child_dna_will_not_verify_under_a_different_platform_key(vclock):
    from darwin_fakes.fake_platform import SigningService

    platform, stack = make_platform(opening_usdc=200.0, clock=vclock)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    asyncio.run(rt.run(max_heartbeats=5))
    rec = _force_reproduction(rt, stack, balance=400.0)

    # A child sealed by the real platform must not boot against an impostor
    # signing key (replay / lineage-binding protection at the next gen too).
    cplat, cstk = make_child_platform(
        signer=SigningService(),  # wrong key
        child_manifest_json=rec["child_manifest"],
        child_layer_blobs=rec["child_layer_blobs"],
        opening_usdc=rec["budget_usdc"],
        clock=vclock,
    )
    with pytest.raises(Exception):
        boot(cplat, clock=vclock, sleep=vclock.sleep)
