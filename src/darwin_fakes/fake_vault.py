"""In-memory vault (RawVaultPort) + owner-grant store. NOT part of the genome.

The real vault is HashiCorp Vault with a namespace per Darwin; the real grant
store is the platform writing a one-time token at
``darwins/{id}/escalations/{eid}/grant`` when the owner approves an escalation
(docs/engineering/16). Here both are dicts — enough to exercise the tiered
vault model end to end.
"""

from __future__ import annotations


class FakeVault:
    def __init__(self) -> None:
        self._kv: dict[str, str] = {}

    def put(self, key: str, value: str) -> None:
        self._kv[key] = value

    def get(self, key: str) -> str | None:
        return self._kv.get(key)


class MemoryOwnerGrantStore:
    """One-time owner-approval tokens. The platform ``grant``s a token when an
    escalation is approved; the genome ``consume``s it exactly once for a
    sensitive read, then it self-destructs (doc 16, KYC)."""

    def __init__(self) -> None:
        self._live: set[str] = set()

    def grant(self, token: str) -> None:
        """Platform side: the owner approved an escalation."""
        self._live.add(token)

    def consume(self, token: str) -> bool:
        """Genome side: True at most once per granted token, then gone."""
        if token in self._live:
            self._live.discard(token)
            return True
        return False
