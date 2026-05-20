"""Platform signing key rotation (docs/engineering/02, 08).

The platform's Ed25519 signing key is the trust root for every Darwin
manifest. Production deploys need a procedure to rotate it (e.g. after
suspected key compromise, scheduled annual rotation, or HSM migration)
WITHOUT invalidating the lineage of every Darwin alive at the time.

Architecture:

- The supervisor holds ONE active signing key at any moment. New
  spawns + new layers are signed with the active key.
- Rotation is a two-phase operation:

    1. ``rotate_signing_key()`` issues a new key, writes it to the
       persistence ``platform_signing_key`` row, and tags the old
       key as ``retired_at=now`` in a new ``retired_keys`` row.
       Every Darwin's existing manifest is STILL signed by the old
       key — that's fine, the trust root tracks multiple acceptable
       public keys during the drain window.

    2. ``verify_with_history(blob, sig)`` checks the signature against
       the active key first, then any retired key whose drain window
       hasn't expired. After the drain window, retired keys are no
       longer accepted — a Darwin whose manifest can't re-sign before
       then must reproduce with the new active key.

The drain window (default 90 days) is wide enough for normal
reproduction cadence. Operators can shorten it for emergency
rotation (compromised key) at the cost of more forced
reproductions."""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_DRAIN_WINDOW_SECONDS = 90 * 86_400.0


class KeyRotationError(Exception):
    """Rotation refused (no persistence) or signature outside drain window."""


@dataclass(frozen=True)
class RetiredKey:
    """An Ed25519 key that's been rotated out but is still accepted
    during the drain window so old manifests verify."""

    public_key_pem: bytes
    retired_at: float
    drain_until: float

    def to_json(self) -> dict:
        return {
            "public_key_pem": self.public_key_pem.decode("ascii"),
            "retired_at": self.retired_at,
            "drain_until": self.drain_until,
        }


class KeyRotationService:
    """Manages active + retired keys. Backed by the persistence
    layer's :class:`PersistenceStore` via a small interface:

    - ``persistence.save_signing_key(raw_bytes)`` — same call used
      at supervisor first-boot.
    - ``persistence.load_signing_key()``.
    - ``persistence.record_retired_key(record)`` /
      ``persistence.list_retired_keys()`` — added by this module via
      monkey-patching at construction time (keeps persistence.py from
      bloating with rotation-specific schema unless an operator wants
      it).
    """

    def __init__(
        self,
        *,
        persistence: Any,
        signer: Any,
        signer_factory: Callable[[], Any],
        signer_from_bytes: Callable[[bytes], Any],
        drain_window_seconds: float = DEFAULT_DRAIN_WINDOW_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if persistence is None:
            raise KeyRotationError(
                "key rotation requires the persistence layer to be enabled"
            )
        self._persistence = persistence
        self._signer = signer
        self._signer_factory = signer_factory
        self._signer_from_bytes = signer_from_bytes
        self._drain = float(drain_window_seconds)
        self._clock = clock
        self._retired: list[RetiredKey] = self._load_retired()

    @property
    def active_public_key_pem(self) -> bytes:
        return self._signer.public_key_pem()

    @property
    def signer(self) -> Any:
        return self._signer

    def accepted_public_keys(self) -> list[bytes]:
        """The full set of public keys that verify-with-history accepts
        right now. Operators expose this so any external verifier
        (compliance audit, an independent watcher) sees the same set."""
        self._purge_expired()
        keys = [self.active_public_key_pem]
        for r in self._retired:
            keys.append(r.public_key_pem)
        return keys

    def rotate(
        self,
        *,
        new_drain_window_seconds: float | None = None,
    ) -> dict:
        """Issue a new signing key, retire the current one.

        Returns a summary dict with the new public key + drain
        deadline for the old one. The supervisor's audit chain
        records this via the operator audit log; rotation is a
        constitutional event."""
        drain_for = (
            float(new_drain_window_seconds)
            if new_drain_window_seconds is not None
            else self._drain
        )
        old_pem = self._signer.public_key_pem()
        retired_at = self._clock()
        record = RetiredKey(
            public_key_pem=old_pem,
            retired_at=retired_at,
            drain_until=retired_at + drain_for,
        )
        # Persist the retired key BEFORE installing the new one — a
        # crash mid-rotation must not lose the old key, otherwise the
        # entire fleet's manifests become unverifiable.
        self._retired.append(record)
        self._persist_retired()
        # Install the new key.
        new_signer = self._signer_factory()
        new_raw = new_signer.private_bytes()
        # Replace the active key in persistence. Use a low-level
        # update path so we don't depend on save_signing_key's
        # "INSERT...ON CONFLICT DO NOTHING" behaviour (which would
        # refuse to overwrite).
        self._replace_active_key(new_raw)
        self._signer = new_signer
        return {
            "new_public_key_pem": new_signer.public_key_pem().decode("ascii"),
            "retired_public_key_pem": old_pem.decode("ascii"),
            "drain_until": record.drain_until,
        }

    def _purge_expired(self) -> None:
        now = self._clock()
        self._retired = [r for r in self._retired if r.drain_until >= now]
        self._persist_retired()

    # -- persistence helpers --------------------------------------------
    def _load_retired(self) -> list[RetiredKey]:
        backend = self._persistence._backend  # noqa: SLF001
        with backend:
            backend.execute(
                "CREATE TABLE IF NOT EXISTS platform_retired_keys ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "  public_key_pem TEXT NOT NULL, "
                "  retired_at REAL NOT NULL, "
                "  drain_until REAL NOT NULL"
                ")"
            )
        rows = list(backend.execute(
            "SELECT public_key_pem, retired_at, drain_until "
            "FROM platform_retired_keys ORDER BY retired_at ASC"
        ).fetchall())
        out: list[RetiredKey] = []
        for r in rows:
            pem = r["public_key_pem"] if isinstance(r, dict) else r[0]
            retired = r["retired_at"] if isinstance(r, dict) else r[1]
            drain = r["drain_until"] if isinstance(r, dict) else r[2]
            out.append(RetiredKey(
                public_key_pem=pem.encode("ascii") if isinstance(pem, str) else pem,
                retired_at=float(retired),
                drain_until=float(drain),
            ))
        return out

    def _persist_retired(self) -> None:
        backend = self._persistence._backend  # noqa: SLF001
        with backend:
            backend.execute("DELETE FROM platform_retired_keys")
            for r in self._retired:
                backend.execute(
                    "INSERT INTO platform_retired_keys "
                    "(public_key_pem, retired_at, drain_until) "
                    "VALUES (?, ?, ?)",
                    (
                        r.public_key_pem.decode("ascii"),
                        float(r.retired_at),
                        float(r.drain_until),
                    ),
                )

    def _replace_active_key(self, raw: bytes) -> None:
        backend = self._persistence._backend  # noqa: SLF001
        with backend:
            backend.execute(
                "DELETE FROM platform_signing_key WHERE id = 1"
            )
            backend.execute(
                "INSERT INTO platform_signing_key "
                "(id, private_key, created_at) VALUES (1, ?, ?)",
                (
                    base64.b64encode(raw).decode("ascii"),
                    float(self._clock()),
                ),
            )
