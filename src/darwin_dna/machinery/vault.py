"""Tiered vault access (docs/engineering/16 "Tiered access", 07).

The platform vault is a per-Darwin namespace; cross-namespace reads are made
*impossible* by the infrastructure, not by this code (doc 16). What the genome
*does* enforce, on top of that, is the three-tier read model:

    public     — any neuron in this Darwin may read.
    scoped     — only a neuron whose capability tag matches the secret's
                 scope segment may read (the AppRole-per-capability model).
    sensitive  — only with a fresh, single-use owner-issued grant token,
                 which self-destructs after one read (doc 16, KYC).

Writes are always allowed (a Darwin writes the credentials for accounts it
opens — doc 16 "the agent writes secrets"), but they must name their tier
explicitly: an un-tiered secret is a secret nobody set the blast radius for.

This is the single genome primitive for that policy. The raw vault port and
the owner-grant store are platform-provided and untrusted-but-authoritative;
the genome can *consume* a grant, it can never mint one.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable


class VaultTier(str, Enum):
    PUBLIC = "public"
    SCOPED = "scoped"
    SENSITIVE = "sensitive"


class VaultAccessError(Exception):
    """A read was refused by the tier model. Fail closed and loud: a silent
    empty read of a credential is how a neuron ends up acting unauthenticated
    and looking like a different failure three nodes later."""


@runtime_checkable
class RawVaultPort(Protocol):
    """The platform's per-Darwin KV namespace. Path isolation across Darwins
    is enforced by the vault itself (doc 16); this is just get/put."""

    def put(self, key: str, value: str) -> None: ...

    def get(self, key: str) -> str | None: ...


@runtime_checkable
class OwnerGrantStore(Protocol):
    """Where the platform writes one-time owner-approval tokens when an
    escalation is approved (doc 16: ``darwins/{id}/escalations/{eid}/grant``).
    ``consume`` returns True at most once per token, then it self-destructs."""

    def consume(self, token: str) -> bool: ...


def _seg(s: str) -> str:
    """A path segment with the tier separators stripped, so a key can never
    forge its own tier/scope by smuggling a '/' into it."""
    return str(s).replace("/", "_").strip()


class TieredVault:
    """Wraps a raw per-Darwin vault and enforces the tier read model. One per
    Darwin; neurons never touch this directly — they get a
    :class:`ScopedVaultView` bound to their own capability tags."""

    def __init__(
        self,
        raw: RawVaultPort,
        *,
        darwin_id: str,
        grants: OwnerGrantStore | None = None,
    ):
        self._raw = raw
        self._id = _seg(darwin_id)
        self._grants = grants

    def _path(self, tier: VaultTier, scope: str | None, key: str) -> str:
        base = f"darwins/{self._id}/{tier.value}"
        if tier is VaultTier.SCOPED:
            return f"{base}/{_seg(scope or 'shared')}/{_seg(key)}"
        return f"{base}/{_seg(key)}"

    def put(
        self,
        tier: VaultTier,
        key: str,
        value: str,
        *,
        scope: str | None = None,
    ) -> None:
        self._raw.put(self._path(tier, scope, key), str(value))

    def read(
        self,
        tier: VaultTier,
        key: str,
        *,
        neuron_tags: frozenset[str],
        scope: str | None = None,
        grant_token: str | None = None,
    ) -> str | None:
        if tier is VaultTier.PUBLIC:
            pass  # any neuron in this Darwin may read public
        elif tier is VaultTier.SCOPED:
            sc = scope or "shared"
            if sc not in neuron_tags:
                raise VaultAccessError(
                    f"scoped secret {sc!r} not in this neuron's capabilities"
                )
        elif tier is VaultTier.SENSITIVE:
            # Fail closed: no grant port, no token, or a spent/forged token
            # are all the same answer — no.
            if self._grants is None or not grant_token:
                raise VaultAccessError(
                    "sensitive read requires a fresh owner grant token"
                )
            if not self._grants.consume(grant_token):
                raise VaultAccessError(
                    "owner grant token is invalid or already spent"
                )
        return self._raw.get(self._path(tier, scope, key))


class ScopedVaultView:
    """The per-invocation vault handle a neuron is actually given (doc 04,
    isolation). It carries *only* this neuron's capability tags, so the tier
    model is enforced from the neuron's own authority — the neuron cannot ask
    for a scope it was not granted because it never holds the others.

    Backward-compatible surface: ``put(key, value)`` still works (defaults to
    the SCOPED tier under this neuron's own scope) so a neuron that just wants
    to persist a credential it acquired does not need to know the tier model;
    a neuron that does care passes ``tier=`` explicitly."""

    def __init__(
        self,
        tiered: TieredVault,
        *,
        capability_tags: frozenset[str],
        default_scope: str,
    ):
        self._v = tiered
        self._tags = frozenset(capability_tags)
        self._default_scope = default_scope

    def put(
        self,
        key: str,
        value: str,
        *,
        tier: VaultTier = VaultTier.SCOPED,
        scope: str | None = None,
    ) -> None:
        self._v.put(
            tier, key, value,
            scope=(scope or self._default_scope) if tier is VaultTier.SCOPED
            else None,
        )

    def get(
        self,
        key: str,
        *,
        tier: VaultTier = VaultTier.SCOPED,
        scope: str | None = None,
        grant_token: str | None = None,
    ) -> str | None:
        return self._v.read(
            tier,
            key,
            neuron_tags=self._tags,
            scope=(scope or self._default_scope)
            if tier is VaultTier.SCOPED
            else None,
            grant_token=grant_token,
        )
