"""Deep lineage (docs/engineering/06).

Reproduction is only proven if a *descendant several generations down* still
boots through the full trust root. This walks a lineage to depth 4, booting
and verifying every generation, and checks that each generation re-seals all
inherited layers under its own identity and accumulates exactly one distilled
lesson layer.
"""

from __future__ import annotations

import asyncio

from darwin_dna.evolution import boot
from darwin_fakes import make_child_platform, make_platform

DEPTH = 4


def test_lineage_to_depth_four_boots_and_verifies(vclock):
    platform, stack = make_platform(opening_usdc=1000.0, clock=vclock)
    cur = boot(platform, clock=vclock, sleep=vclock.sleep)
    cur_stack = stack

    seen = []
    for _ in range(DEPTH):
        asyncio.run(cur.run(max_heartbeats=12))

        # Every generation's DNA must verify (boot ran the trust root) and
        # carry exactly generation+1 sealed layers.
        gen = cur.identity.generation
        assert len(cur._stack._by_layer) == gen + 1
        assert len(cur.identity.lineage) == gen
        assert len(cur._stack.all_lessons()) == gen  # one lesson per ancestor
        cur.audit.verify()
        seen.append(gen)

        cur.state.balance_usdc = 900.0
        cur_stack.wallet._balance = 900.0
        cur.state.profit_baseline_usdc = 50.0
        cur.state.reproduce = True
        cur._reproduce()
        rec = cur_stack.spawner.children[-1]

        cplat, cstack = make_child_platform(
            signer=cur_stack.signer,  # same platform key down the whole line
            child_manifest_json=rec["child_manifest"],
            child_layer_blobs=rec["child_layer_blobs"],
            opening_usdc=rec["budget_usdc"],
            clock=vclock,
        )
        parent_id = cur.identity.darwin_id
        cur = boot(cplat, clock=vclock, sleep=vclock.sleep)
        cur_stack = cstack
        # The freshly booted child is the next generation, child of its parent.
        assert cur.identity.generation == gen + 1
        assert cur.identity.parent_id == parent_id

    assert seen == list(range(DEPTH))  # 0,1,2,3 booted in order
    assert cur.identity.generation == DEPTH  # the final child is gen 4

    st = asyncio.run(cur.run(max_heartbeats=8))
    assert st.alive
    cur.audit.verify()
    # Distilled lessons are attributed to the generation that learned them.
    born = sorted(l.born_generation for l in cur._stack.all_lessons())
    assert born == list(range(1, DEPTH + 1))
