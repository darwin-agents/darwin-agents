"""Identity & lineage primitives (docs/engineering/02-dna-core.md).

``Identity`` is the *who* of a Darwin — bound into the signed manifest so a
layer cannot be replayed into a different lineage. ``uuid7`` is the
time-ordered identifier the genome uses for darwin_id so audit traces sort
by birth.
"""

from __future__ import annotations

import time

import pytest

from darwin_dna.identity import Identity, uuid7


# -- uuid7 --------------------------------------------------------------------


def test_uuid7_is_version_7_and_rfc_variant():
    u = uuid7()
    assert u.version == 7
    # RFC variant: high two bits of clock_seq_hi are '10'.
    assert u.variant == "specified in RFC 4122"


def test_uuid7_is_time_ordered_within_a_run():
    """Two uuid7s minted in order must sort in the same order — this is the
    property the platform relies on for audit-trail sortability."""
    a = uuid7()
    time.sleep(0.005)
    b = uuid7()
    assert str(a) < str(b)


def test_uuid7_is_unique_across_many_mints():
    n = 200
    ids = {uuid7() for _ in range(n)}
    assert len(ids) == n


# -- Identity ----------------------------------------------------------------


def test_genesis_has_no_parent_and_empty_lineage():
    ident = Identity.genesis(l0_version="0.0.1")
    assert ident.generation == 0
    assert ident.parent_id is None
    assert ident.lineage == []
    assert ident.l0_version == "0.0.1"


def test_genesis_birth_ts_is_recent():
    ident = Identity.genesis(l0_version="0.0.1")
    assert abs(time.time() - ident.birth_ts) < 5.0


def test_child_increments_generation_and_extends_lineage():
    parent = Identity.genesis(l0_version="0.0.1")
    child = parent.child(l0_version="0.0.1")
    assert child.generation == parent.generation + 1
    assert child.parent_id == parent.darwin_id
    assert child.lineage == [parent.darwin_id]


def test_grandchild_records_full_ancestry_oldest_first():
    g = Identity.genesis(l0_version="0.0.1")
    c = g.child(l0_version="0.0.1")
    gc = c.child(l0_version="0.0.1")
    assert gc.lineage == [g.darwin_id, c.darwin_id]
    assert gc.lineage[0] == g.darwin_id  # oldest first


def test_identity_is_frozen():
    """Identity is established at birth; the running Darwin must not be able
    to rewrite it (P1)."""
    ident = Identity.genesis(l0_version="0.0.1")
    with pytest.raises(Exception):
        ident.generation = 999  # type: ignore[misc]


def test_negative_generation_refused():
    with pytest.raises(Exception):
        Identity(
            darwin_id="d-1",
            generation=-1,
            parent_id=None,
            birth_ts=0.0,
            lineage=[],
            l0_version="0.0.1",
        )
