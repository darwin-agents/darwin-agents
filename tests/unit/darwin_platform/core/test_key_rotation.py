"""Tests for platform signing-key rotation (doc 02 §"rotation")."""

from __future__ import annotations

from pathlib import Path

import pytest

from darwin_fakes.fake_platform import SigningService
from darwin_platform.key_rotation import (
    KeyRotationError,
    KeyRotationService,
)
from darwin_platform.persistence import PersistenceStore


def _service(tmp_path: Path) -> tuple[KeyRotationService, PersistenceStore, SigningService]:
    store = PersistenceStore(tmp_path / "db.sqlite")
    # Initial signing key — write it to the store the way Supervisor
    # would on first boot.
    signer = SigningService()
    store.save_signing_key(signer.private_bytes())
    svc = KeyRotationService(
        persistence=store,
        signer=signer,
        signer_factory=SigningService,
        signer_from_bytes=SigningService.from_private_bytes,
    )
    return svc, store, signer


def test_rotation_returns_new_keys(tmp_path):
    svc, store, original_signer = _service(tmp_path)
    summary = svc.rotate()
    assert "new_public_key_pem" in summary
    assert "retired_public_key_pem" in summary
    assert summary["retired_public_key_pem"] != summary["new_public_key_pem"]
    store.close()


def test_rotation_persists_active_key_change(tmp_path):
    svc, store, original_signer = _service(tmp_path)
    pem_before = original_signer.public_key_pem()
    svc.rotate()
    # Persisted active key has changed.
    raw = store.load_signing_key()
    new_signer = SigningService.from_private_bytes(raw)
    assert new_signer.public_key_pem() != pem_before
    store.close()


def test_accepted_keys_includes_retired_during_drain(tmp_path):
    svc, store, _ = _service(tmp_path)
    pem_before = svc.active_public_key_pem
    svc.rotate()
    accepted = svc.accepted_public_keys()
    # Both the new active key and the retired one are accepted while
    # within the drain window.
    assert any(pem == pem_before for pem in accepted)
    assert svc.active_public_key_pem in accepted
    store.close()


def test_retired_key_purged_after_drain_window(tmp_path):
    """After drain_until passes, the retired key is no longer accepted."""
    now = [1000.0]
    store = PersistenceStore(tmp_path / "db.sqlite")
    signer = SigningService()
    store.save_signing_key(signer.private_bytes())
    svc = KeyRotationService(
        persistence=store, signer=signer,
        signer_factory=SigningService,
        signer_from_bytes=SigningService.from_private_bytes,
        drain_window_seconds=10.0,
        clock=lambda: now[0],
    )
    pem_old = svc.active_public_key_pem
    svc.rotate()
    # Within window — both accepted.
    assert any(pem == pem_old for pem in svc.accepted_public_keys())
    # Past the window — old key purged.
    now[0] = 2000.0
    accepted = svc.accepted_public_keys()
    assert pem_old not in accepted
    assert svc.active_public_key_pem in accepted
    store.close()


def test_multiple_rotations_stack_retired_keys(tmp_path):
    svc, store, _ = _service(tmp_path)
    svc.rotate()
    svc.rotate()
    accepted = svc.accepted_public_keys()
    # Active + two retired
    assert len(accepted) == 3
    store.close()


def test_rotation_requires_persistence():
    with pytest.raises(KeyRotationError):
        KeyRotationService(
            persistence=None, signer=SigningService(),
            signer_factory=SigningService,
            signer_from_bytes=SigningService.from_private_bytes,
        )


def test_rotation_persists_retired_across_restart(tmp_path):
    """The retired-keys table survives a process restart so a fresh
    supervisor instance still accepts a recently-retired key."""
    store = PersistenceStore(tmp_path / "db.sqlite")
    signer = SigningService()
    store.save_signing_key(signer.private_bytes())
    svc = KeyRotationService(
        persistence=store, signer=signer,
        signer_factory=SigningService,
        signer_from_bytes=SigningService.from_private_bytes,
    )
    svc.rotate()
    store.close()

    # "Restart" — re-open the store, rebuild service.
    store2 = PersistenceStore(tmp_path / "db.sqlite")
    raw_active = store2.load_signing_key()
    signer2 = SigningService.from_private_bytes(raw_active)
    svc2 = KeyRotationService(
        persistence=store2, signer=signer2,
        signer_factory=SigningService,
        signer_from_bytes=SigningService.from_private_bytes,
    )
    # Retired key still in the accepted set after restart.
    assert len(svc2.accepted_public_keys()) >= 2
    store2.close()
