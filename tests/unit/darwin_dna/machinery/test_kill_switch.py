"""Paranoid kill-switch tests (docs/engineering/18 hot path #2, P8)."""

from __future__ import annotations

import pytest

from darwin_dna.machinery.kill_switch import KillSignal, KillSwitch
from darwin_fakes.fake_platform import KillFlag


def test_check_passes_while_alive():
    ks = KillSwitch(KillFlag())
    ks.check()  # no raise
    assert not ks.tripped()


def test_flip_then_check_raises():
    flag = KillFlag()
    ks = KillSwitch(flag)
    flag.flip("operator pulled it")
    with pytest.raises(KillSignal) as ei:
        ks.check()
    assert "operator pulled it" in ei.value.reason
    assert ks.tripped()


def test_kill_is_final_and_idempotent():
    flag = KillFlag()
    ks = KillSwitch(flag)
    flag.flip()
    with pytest.raises(KillSignal):
        ks.check()
    # Even if the flag were somehow cleared, a kill is final.
    flag._killed = False
    with pytest.raises(KillSignal):
        ks.check()


def test_kill_signal_is_baseexception_not_exception():
    # An adversarial `except Exception:` in soma-evolved code must NOT be able
    # to swallow a kill.
    assert issubclass(KillSignal, BaseException)
    assert not issubclass(KillSignal, Exception)
    flag = KillFlag()
    ks = KillSwitch(flag)
    flag.flip()
    swallowed = False
    try:
        try:
            ks.check()
        except Exception:  # noqa: BLE001 - intentional adversarial catch
            swallowed = True
    except KillSignal:
        pass
    assert swallowed is False


def test_requires_a_kill_source():
    with pytest.raises(TypeError):
        KillSwitch(object())


class _UnreachableSource:
    """A kill source whose every poll fails — a control-plane outage."""

    def __init__(self, exc: BaseException | None = None):
        self._exc = exc or ConnectionError("control plane unreachable")

    def is_killed(self) -> bool:
        raise self._exc

    def reason(self) -> str | None:
        raise self._exc


class _FlakySource:
    """Unreachable on the first poll, then healthy and reporting alive."""

    def __init__(self):
        self.polls = 0

    def is_killed(self) -> bool:
        self.polls += 1
        if self.polls == 1:
            raise TimeoutError("first poll timed out")
        return False

    def reason(self) -> str | None:
        return None


def test_unreachable_source_fails_closed():
    # docs 24 & 28: a source we cannot reach must be treated as killed.
    ks = KillSwitch(_UnreachableSource())
    with pytest.raises(KillSignal) as ei:
        ks.check()
    assert "unreachable" in ei.value.reason
    assert ks.tripped()


def test_unreachable_then_recovered_stays_killed():
    # A flaky source that recovers and says "alive" must not un-kill: a kill
    # is final, however it was triggered.
    src = _FlakySource()
    ks = KillSwitch(src)
    with pytest.raises(KillSignal):
        ks.check()  # first poll raises -> trips
    with pytest.raises(KillSignal):
        ks.check()  # source now healthy & alive, but the kill stands
    assert src.polls == 1  # second check never re-polled the source


def test_reason_failure_does_not_block_a_real_kill():
    # is_killed() reports True but reason() throws — the kill must still fire.
    class _KilledButReasonless:
        def is_killed(self) -> bool:
            return True

        def reason(self) -> str | None:
            raise ConnectionError("no reason endpoint")

    ks = KillSwitch(_KilledButReasonless())
    with pytest.raises(KillSignal):
        ks.check()
    assert ks.tripped()


def test_fail_closed_kill_is_still_baseexception():
    # The fail-closed path must raise the same uncatchable signal as a normal
    # kill — soma code cannot swallow it with `except Exception`.
    ks = KillSwitch(_UnreachableSource())
    swallowed = False
    try:
        try:
            ks.check()
        except Exception:  # noqa: BLE001 - intentional adversarial catch
            swallowed = True
    except KillSignal:
        pass
    assert swallowed is False
