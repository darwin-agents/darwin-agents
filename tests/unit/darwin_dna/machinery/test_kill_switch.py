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
