"""Real secrets-vault backend adapter (docs/engineering/16).

The genome's :class:`TieredVault` (in ``darwin_dna.machinery.vault``) sits
on top of a tiny KV interface: ``put(key, value) / get(key)``. The fake
stack stores that KV in a Python dict; the real platform stores it in
HashiCorp Vault with a namespace per Darwin.

This module ships the platform-side adapter:

- :class:`VaultClient` Protocol — the small surface the adapter needs.
  Three methods: ``kv_get``, ``kv_put``, ``kv_delete``. Production wires
  this to ``hvac`` (HashiCorp's Python client); tests inject a stub.
- :class:`RealVaultBackend` — implements the same ``put`` / ``get``
  surface as :class:`FakeVault` so it's a drop-in. Each darwin's
  namespace is a path prefix (``darwins/{id}/...``) that the platform
  controls; the backend just stores keys verbatim, leaving namespace
  composition to the caller (the genome's ``TieredVault`` already
  composes ``darwins/{id}/{tier}/...``).

Production deployers wire :class:`HvacVaultClient` (not built here yet;
follow-up slice). The Protocol is the public surface a real wrapper
plugs into. KYC documents (the highest-sensitivity class) are written
to ``sensitive/kyc/...`` by the genome via the tiered vault; the
adapter writes through to the same Vault namespace transparently.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class VaultError(Exception):
    """Raised on backend errors that the caller can choose to surface
    or escalate. The genome's ``TieredVault`` already catches this and
    fails closed for sensitive-tier reads."""


@runtime_checkable
class VaultClient(Protocol):
    """Tiny surface around a real KV backend. ``hvac`` (HashiCorp Vault)
    wraps cleanly behind these three methods; tests inject a stub."""

    def kv_put(self, key: str, value: str) -> None: ...

    def kv_get(self, key: str) -> str | None: ...

    def kv_delete(self, key: str) -> None: ...


class RealVaultBackend:
    """Drop-in for :class:`FakeVault` against a real :class:`VaultClient`.

    The genome's ``TieredVault`` already composes paths like
    ``darwins/{id}/{tier}/[scope/]key``; the backend stores those keys
    verbatim. A real Vault deployment would map the top-level
    ``darwins/{id}/`` prefix to a per-Darwin namespace so cross-tenant
    reads are physically impossible (doc 16 §"Namespace per Darwin");
    that mapping lives in the :class:`VaultClient` impl, not here, so
    it stays test-friendly.

    Errors from the backend are surfaced as :class:`VaultError`. The
    genome's sensitive-tier path already handles "no value" by failing
    closed; the adapter's ``get`` returns ``None`` for both genuinely
    missing keys and read failures (with a debug-level log signal a
    real impl can emit).
    """

    def __init__(self, client: VaultClient) -> None:
        if not isinstance(client, VaultClient):
            raise TypeError("RealVaultBackend requires a VaultClient")
        self._client = client

    def put(self, key: str, value: str) -> None:
        try:
            self._client.kv_put(str(key), str(value))
        except Exception as exc:
            raise VaultError(f"vault put({key!r}) failed: {exc}") from exc

    def get(self, key: str) -> str | None:
        try:
            v = self._client.kv_get(str(key))
        except Exception:
            # Fail closed without leaking the backend's exception shape;
            # the genome's tiered vault treats None as "no such key".
            return None
        return None if v is None else str(v)

    def delete(self, key: str) -> None:
        """Not part of the genome's surface yet, but real ops will need
        it (e.g. revoking a credential per doc 09's owner controls).
        Refused silently when the backend has no delete primitive."""
        try:
            self._client.kv_delete(str(key))
        except Exception as exc:
            raise VaultError(f"vault delete({key!r}) failed: {exc}") from exc


class InMemoryVaultClient:
    """Stub :class:`VaultClient` for tests + local dev. Same shape as the
    production hvac wrapper would expose; lets us exercise the adapter
    end-to-end without standing up a Vault server."""

    def __init__(self) -> None:
        self._kv: dict[str, str] = {}

    def kv_put(self, key: str, value: str) -> None:
        self._kv[key] = value

    def kv_get(self, key: str) -> str | None:
        return self._kv.get(key)

    def kv_delete(self, key: str) -> None:
        self._kv.pop(key, None)
