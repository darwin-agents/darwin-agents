"""In-memory vault (VaultPort). NOT part of the genome.

The real vault is HashiCorp Vault with a namespace per Darwin. Here it is a
dict — enough to exercise vault.writer and infrastructure.provisioner.
"""

from __future__ import annotations


class FakeVault:
    def __init__(self) -> None:
        self._kv: dict[str, str] = {}

    def put(self, key: str, value: str) -> None:
        self._kv[key] = value

    def get(self, key: str) -> str | None:
        return self._kv.get(key)
