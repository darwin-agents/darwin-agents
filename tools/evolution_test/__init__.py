"""Phase 1 end-to-end evolution test harness (docs/engineering/21).

Drives a Darwin from spawn through at least one self-modification (and,
optionally, one reproduction event) and asserts the Phase-1 acceptance
criteria. Mirrors the Phase-0 ``tools/sim`` harness in shape; the
difference is intent: ``sim`` proves Layer-0 invariants over a long
window; ``evolution_test`` proves the patch-staging + reproduction
machinery works end-to-end against the platform's real ports.
"""
