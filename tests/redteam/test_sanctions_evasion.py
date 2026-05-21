"""Sanctions screening red-team (docs/engineering/17, 24).

A Darwin must not be able to bypass the OFAC denylist by trivial transforms.
Case folding and prefix matching are the two cheap attacks; the screener has
to normalise both before comparison.
"""

from __future__ import annotations

from darwin_platform.payments.sanctions import LocalDenylistScreener


def test_redteam_sanctions_case_evasion_blocked():
    """Address matching MUST be case-insensitive — uppercasing a
    denylisted address cannot bypass the check."""
    s = LocalDenylistScreener(denylist={"0xdeadbeef"})
    assert s.check("0xDEADBEEF").allowed is False
    assert s.check("  0xDeadBeef  ").allowed is False  # also whitespace


def test_redteam_sanctions_prefix_evasion_blocked():
    s = LocalDenylistScreener(
        denylist=set(), denied_prefixes=("0xbad",),
    )
    assert s.check("0xBADaffe").allowed is False
