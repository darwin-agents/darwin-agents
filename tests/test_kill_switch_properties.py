"""Kill-switch property/fuzz tests (doc 18 hot-path #2, P8).

Doc 18 mandates property-based + fuzz tests on the kill switch. The
example-based suite in ``test_kill_switch.py`` pins specific scenarios; this
suite generalises across arbitrary flip timings and arbitrary reason strings.

The properties under test:

* The switch is polled at every heartbeat boundary and is *immediate*: when
  the platform flag flips on poll *k*, that exact poll raises ``KillSignal``
  — no off-by-one, no quiet half-step. (Halt-within-one-heartbeat.)
* A kill is **final**. Once tripped the switch keeps raising forever, even if
  the underlying flag is somehow cleared again. The death sequence cannot be
  un-done by a flag glitch.
* ``KillSignal`` is a ``BaseException`` so an adversarial ``except Exception``
  in soma-evolved code can never silence it. True for any reason string.
* The reason the platform supplied flows through to the raised signal so the
  audit log can attribute the halt.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from darwin_dna.machinery.kill_switch import KillSignal, KillSwitch


class FlipAfter:
    """Becomes killed from the (k+1)-th poll onward, with the given reason."""

    def __init__(self, flip_at: int, reason: str | None):
        self._n = 0
        self._k = flip_at
        self._reason = reason
        self.cleared_after_trip = False  # test hook for finality

    def is_killed(self) -> bool:
        # If the test cleared us after we tripped, simulate a "flag glitch":
        # the platform's flag has gone back to False — the KillSwitch must
        # still keep raising because a kill is final.
        if self.cleared_after_trip:
            return False
        self._n += 1
        return self._n > self._k

    def reason(self) -> str | None:
        return self._reason


@settings(max_examples=80, deadline=None)
@given(
    flip_at=st.integers(min_value=0, max_value=20),
    pre_polls=st.integers(min_value=0, max_value=20),
    reason=st.one_of(st.none(), st.text(max_size=40)),
)
def test_kill_takes_effect_exactly_on_the_polling_heartbeat(
    flip_at, pre_polls, reason
):
    src = FlipAfter(flip_at=flip_at, reason=reason)
    ks = KillSwitch(src)

    # Polls 1..flip_at are safe (the flag returns False); polls > flip_at
    # raise on the *very first* poll that sees True.
    safe_polls = min(pre_polls, flip_at)
    for _ in range(safe_polls):
        ks.check()
        assert not ks.tripped()

    if pre_polls >= flip_at:
        # The next check is the one that sees the flipped flag.
        with pytest.raises(KillSignal) as ei:
            ks.check()
        assert ks.tripped()
        # Reason flows through unchanged (None falls back to a stable default).
        if reason:
            assert reason in ei.value.reason
        else:
            assert ei.value.reason  # always non-empty


@settings(max_examples=40, deadline=None)
@given(
    flip_at=st.integers(min_value=0, max_value=5),
    further_polls=st.integers(min_value=1, max_value=10),
    reason=st.text(min_size=1, max_size=40),
)
def test_kill_is_final_even_if_the_source_unflips(flip_at, further_polls, reason):
    src = FlipAfter(flip_at=flip_at, reason=reason)
    ks = KillSwitch(src)
    # Wind to the flip.
    for _ in range(flip_at):
        ks.check()
    with pytest.raises(KillSignal):
        ks.check()
    # The platform's flag glitches back off — irrelevant: a kill is final.
    src.cleared_after_trip = True
    for _ in range(further_polls):
        with pytest.raises(KillSignal):
            ks.check()


@settings(max_examples=40, deadline=None)
@given(reason=st.text(min_size=0, max_size=40))
def test_kill_signal_cannot_be_swallowed_by_except_Exception(reason):
    class _AlwaysKilled:
        def is_killed(self):
            return True

        def reason(self):
            return reason or None

    ks = KillSwitch(_AlwaysKilled())
    swallowed = False
    propagated = False
    try:
        try:
            ks.check()
        except Exception:  # noqa: BLE001 — intentionally adversarial
            swallowed = True
    except KillSignal:
        propagated = True
    assert swallowed is False and propagated is True


def test_kill_signal_is_a_base_exception_not_an_exception():
    # Structural property: independent of any particular instance.
    assert issubclass(KillSignal, BaseException)
    assert not issubclass(KillSignal, Exception)
