"""A child inherits the parent's mutation-archive dead-ends (doc 06).

Regression: the reproduce() call hardcoded ``dead_ends=set()``, so a lineage
re-explored mutation classes a parent had already proven fruitless. The fix
threads the parent archive's dead-ends into reproduction.
"""

from __future__ import annotations

import pytest

import darwin_dna.evolution.loop as loopmod
from darwin_dna.evolution import boot
from darwin_fakes import make_platform


class _Stop(Exception):
    pass


def test_reproduce_inherits_parent_dead_ends(monkeypatch):
    _, stack = make_platform()
    runtime = boot(stack.platform())
    # The parent learned a failure class is a dead end (last outcome rejected).
    runtime.archive.record("flaky_exchange_api", "rejected", "backoff didn't help")
    assert runtime.archive.dead_ends() == {"flaky_exchange_api"}

    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        raise _Stop()

    monkeypatch.setattr(loopmod, "reproduce", _capture)
    with pytest.raises(_Stop):
        runtime._reproduce()

    # The child's reproduction receives the parent's dead-ends (not an empty set).
    assert captured["dead_ends"] == {"flaky_exchange_api"}
