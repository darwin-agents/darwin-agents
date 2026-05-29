"""Owner-scoped registry of verified fiat-payout destinations.

A Darwin's owner registers each freelancer / contractor / vendor they
might want to pay in fiat. The registry maps an opaque ``freelancer_id``
(owner-chosen) to a verified fiat destination (a Stripe Connect account
id, MoonPay payout token, or whatever the configured FiatRamp's
``withdraw(destination=...)`` accepts).

Why a registry instead of raw destinations on every call:

- **KYC gate at the platform edge.** The owner verifies the freelancer
  once (Stripe Connect Express onboarding, KYC at registration), and
  the registration carries the proof that this destination is safe to
  pay. ``darwin_fiat_payout`` rejects any destination that isn't in
  the registry for the requesting owner.
- **Owner scope.** Each entry is keyed on ``(owner_subject,
  freelancer_id)``, so user A can't fiat-route through user B's
  registered freelancers even if A learns B's freelancer_id.
- **Auditability.** A registered entry has metadata (display name,
  kyc status, registered_at, optional notes) that lands in the
  audit log on every payout — the operator timeline shows who, why,
  and when each fiat hop landed.

The Protocol shape mirrors the pre-approval store: register / revoke /
lookup / list, in-memory default, SQL-backed when persistence is
configured.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class FreelancerDestination:
    """One verified payout destination scoped to an owner."""

    owner_subject: str
    freelancer_id: str          # owner-chosen identifier; unique per owner
    fiat_destination: str       # ramp-specific (Stripe Connect acct id, etc.)
    display_name: str = ""
    kyc_status: str = "unverified"  # "unverified" | "pending" | "verified"
    registered_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class FreelancerRegistryError(Exception):
    """Refused: unknown id, scope mismatch, or backend failure."""


@runtime_checkable
class FreelancerRegistry(Protocol):
    """The minimal surface the supervisor calls. All methods scope by
    ``owner_subject`` so a misuse can't cross owners."""

    def register(self, dest: FreelancerDestination) -> FreelancerDestination: ...

    def lookup(
        self, *, owner_subject: str, freelancer_id: str,
    ) -> FreelancerDestination | None: ...

    def list(
        self, *, owner_subject: str,
    ) -> list[FreelancerDestination]: ...

    def revoke(
        self, *, owner_subject: str, freelancer_id: str,
    ) -> bool: ...


class InMemoryFreelancerRegistry:
    """Process-local registry. The dev / 90-day sim default. Lost on
    restart; for production wire the persistent variant from
    :mod:`darwin_platform.runtime.persistence`."""

    def __init__(self) -> None:
        # Two-level dict: owner_subject -> freelancer_id -> destination.
        self._by_owner: dict[str, dict[str, FreelancerDestination]] = {}

    def register(self, dest: FreelancerDestination) -> FreelancerDestination:
        if not dest.owner_subject:
            raise FreelancerRegistryError("owner_subject is required")
        if not dest.freelancer_id:
            raise FreelancerRegistryError("freelancer_id is required")
        if not dest.fiat_destination:
            raise FreelancerRegistryError("fiat_destination is required")
        self._by_owner.setdefault(
            dest.owner_subject, {}
        )[dest.freelancer_id] = dest
        return dest

    def lookup(
        self, *, owner_subject: str, freelancer_id: str,
    ) -> FreelancerDestination | None:
        bucket = self._by_owner.get(owner_subject)
        if bucket is None:
            return None
        return bucket.get(freelancer_id)

    def list(
        self, *, owner_subject: str,
    ) -> list[FreelancerDestination]:
        bucket = self._by_owner.get(owner_subject, {})
        # Deterministic order so an operator-facing list view stays stable.
        return [bucket[k] for k in sorted(bucket)]

    def revoke(
        self, *, owner_subject: str, freelancer_id: str,
    ) -> bool:
        bucket = self._by_owner.get(owner_subject)
        if bucket is None:
            return False
        return bucket.pop(freelancer_id, None) is not None
