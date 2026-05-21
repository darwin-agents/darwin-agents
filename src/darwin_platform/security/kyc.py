"""KYC provider integration (docs/engineering/22 §"KYC integration").

Before a user can fund a Darwin (or before a Darwin can perform any
identity-sensitive action — KYC writes, large outbound transfers, fiat
withdrawals), the user must pass KYC. Doc 22 selects Persona (or Onfido
— evaluated at Phase 2 kickoff) as the provider.

This module ships:

* :class:`KYCProvider` — Protocol the platform consumes. Real impls wrap
  Persona / Onfido / Sumsub / etc.
* :class:`StubKYCProvider` — in-memory deterministic provider for tests
  and the 90-day sim. Default-allows; supports per-subject overrides.
* :class:`PersonaKYCProvider` — HTTP-API skeleton against Persona's v1
  inquiries API. Optional httpx dependency; falls back to a clean error
  at construction if absent.
* :class:`KYCService` — the platform-side service that caches verdicts,
  records them on the operator audit log, and exposes status queries
  for the API + supervisor's pre-action gates.

KYC documents (ID scan, selfie, utility bill) NEVER pass through this
module. They live in the user's vault under ``tier=sensitive`` and the
KYC provider only sees the inquiry id + verdict — this module never
holds personally-identifying media.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable


class KYCError(Exception):
    """Provider or service error. The platform refuses the gated action
    rather than guessing — KYC is the authority on identity, not the
    platform's optimistic fallback."""


class KYCStatus(str, Enum):
    """The set of states a KYC inquiry can be in. The platform gates
    actions on ``APPROVED``; everything else is a refusal."""

    UNKNOWN = "unknown"        # never inquired
    PENDING = "pending"        # inquiry created, awaiting provider verdict
    APPROVED = "approved"      # passed; allowed to fund + run
    REJECTED = "rejected"      # failed identity check or risk score
    EXPIRED = "expired"        # passed previously; KYC retention TTL ran out
    REQUIRES_REVIEW = "requires_review"  # provider needs a human reviewer


class KYCTier(str, Enum):
    """How thorough the KYC was. Tier gates per-Darwin spend ceilings
    independently of the platform hard caps."""

    BASIC = "basic"          # ID only — low daily limit
    STANDARD = "standard"    # ID + selfie — default
    ENHANCED = "enhanced"    # ID + selfie + utility bill — for high-volume users


@dataclass(frozen=True)
class KYCVerdict:
    """The provider's verdict for one inquiry. ``reason`` carries the
    provider's free-text explanation (audit-legible; never user-facing
    verbatim — the UI re-phrases)."""

    subject: str
    status: KYCStatus
    tier: KYCTier = KYCTier.STANDARD
    inquiry_id: str = ""
    reason: str = ""
    verified_at: float = 0.0
    expires_at: float | None = None

    def is_allowed(self) -> bool:
        return self.status == KYCStatus.APPROVED

    def to_json(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "status": self.status.value,
            "tier": self.tier.value,
            "inquiry_id": self.inquiry_id,
            "reason": self.reason,
            "verified_at": self.verified_at,
            "expires_at": self.expires_at,
        }


@runtime_checkable
class KYCProvider(Protocol):
    """Adapter surface the platform talks to. Real impls hit Persona /
    Onfido / Sumsub. ``start_inquiry`` returns an inquiry id +
    redirect URL the dashboard uses; ``poll_inquiry`` is the
    webhook-or-poll consumption side; ``revoke`` lets the platform
    short-circuit an approved subject (e.g. on suspected fraud)."""

    def start_inquiry(self, *, subject: str, tier: KYCTier = KYCTier.STANDARD) -> dict[str, Any]:
        """Returns ``{"inquiry_id": str, "redirect_url": str, "status": str}``."""
        ...

    def poll_inquiry(self, inquiry_id: str) -> KYCVerdict: ...

    def revoke(self, *, subject: str, reason: str) -> None: ...


# ---------------------------------------------------------------------------
# Stub provider (tests + sim)
# ---------------------------------------------------------------------------
class StubKYCProvider:
    """Deterministic in-memory provider. Defaults to APPROVED so the
    happy path is exercised without a real backend; supports
    per-subject overrides for tests that need to drive a rejection or
    a pending inquiry."""

    def __init__(
        self,
        *,
        default_status: KYCStatus = KYCStatus.APPROVED,
        default_tier: KYCTier = KYCTier.STANDARD,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._default_status = default_status
        self._default_tier = default_tier
        self._overrides: dict[str, KYCVerdict] = {}
        self._inquiries: dict[str, KYCVerdict] = {}
        self._clock = clock
        self._seq = 0

    def set_override(self, subject: str, verdict: KYCVerdict) -> None:
        """Pre-set the verdict the next ``poll_inquiry`` returns for this
        subject. Test-only entry; the production providers don't expose
        this."""
        self._overrides[subject] = verdict

    def start_inquiry(self, *, subject: str, tier: KYCTier = KYCTier.STANDARD) -> dict[str, Any]:
        self._seq += 1
        inq_id = f"inq-{self._seq:06d}"
        # The override (if any) becomes the verdict the next poll returns;
        # absent override, the default applies.
        base = self._overrides.get(subject) or KYCVerdict(
            subject=subject, status=self._default_status,
            tier=tier, inquiry_id=inq_id,
            reason="stub default",
            verified_at=self._clock(),
        )
        verdict = KYCVerdict(
            subject=base.subject, status=base.status, tier=base.tier,
            inquiry_id=inq_id, reason=base.reason,
            verified_at=base.verified_at or self._clock(),
            expires_at=base.expires_at,
        )
        self._inquiries[inq_id] = verdict
        return {
            "inquiry_id": inq_id,
            "redirect_url": f"https://kyc.stub/inquiry/{inq_id}",
            "status": verdict.status.value,
        }

    def poll_inquiry(self, inquiry_id: str) -> KYCVerdict:
        v = self._inquiries.get(inquiry_id)
        if v is None:
            raise KYCError(f"unknown inquiry: {inquiry_id}")
        return v

    def revoke(self, *, subject: str, reason: str) -> None:
        """Mark every existing inquiry for ``subject`` as REJECTED."""
        for inq_id, v in list(self._inquiries.items()):
            if v.subject == subject:
                self._inquiries[inq_id] = KYCVerdict(
                    subject=subject, status=KYCStatus.REJECTED,
                    tier=v.tier, inquiry_id=inq_id,
                    reason=f"revoked: {reason}",
                    verified_at=self._clock(),
                )


# ---------------------------------------------------------------------------
# Persona provider skeleton
# ---------------------------------------------------------------------------
class PersonaKYCProvider:
    """Persona Inquiries API v1 adapter skeleton.

    Construction requires the API key + a template id; the redirect URL
    is constructed from Persona's hosted-flow URL template. The class
    talks to a configurable HTTP client (``httpx.Client``-shaped) so
    tests can inject a stub.

    Not exercised in the unit tests against the real Persona API (would
    need a network sandbox + credentials). Coverage is via a stub
    transport that returns canned JSON. Production wires a real
    ``httpx.Client``.
    """

    PERSONA_API = "https://api.withpersona.com/api/v1"

    def __init__(
        self,
        *,
        api_key: str,
        template_id: str,
        client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not api_key:
            raise KYCError("PersonaKYCProvider requires api_key")
        if not template_id:
            raise KYCError("PersonaKYCProvider requires template_id")
        if client is None:
            try:
                import httpx  # type: ignore[import]
            except ImportError as exc:
                raise KYCError(
                    "PersonaKYCProvider requires httpx (or an injected "
                    "client): `pip install httpx`"
                ) from exc
            client = httpx.Client(
                base_url=self.PERSONA_API,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Persona-Version": "2023-01-05",
                    "Accept": "application/json",
                },
                timeout=20.0,
            )
        self._client = client
        self._template_id = template_id
        self._clock = clock

    def start_inquiry(
        self, *, subject: str, tier: KYCTier = KYCTier.STANDARD
    ) -> dict[str, Any]:
        try:
            resp = self._client.post(
                "/inquiries",
                json={
                    "data": {
                        "attributes": {
                            "inquiry_template_id": self._template_id,
                            "reference_id": subject,
                            "fields": {"tier": tier.value},
                        }
                    }
                },
            )
        except Exception as exc:
            raise KYCError(f"Persona start_inquiry failed: {exc}") from exc
        if not (200 <= getattr(resp, "status_code", 500) < 300):
            raise KYCError(
                f"Persona returned {getattr(resp, 'status_code', 'unknown')}"
            )
        body = resp.json() if hasattr(resp, "json") else {}
        attrs = (body.get("data", {}) or {}).get("attributes", {}) or {}
        inq_id = (body.get("data", {}) or {}).get("id") or ""
        return {
            "inquiry_id": inq_id,
            "redirect_url": (
                attrs.get("hosted-flow-url")
                or f"https://withpersona.com/verify?inquiry-id={inq_id}"
            ),
            "status": attrs.get("status", "pending"),
        }

    def poll_inquiry(self, inquiry_id: str) -> KYCVerdict:
        try:
            resp = self._client.get(f"/inquiries/{inquiry_id}")
        except Exception as exc:
            raise KYCError(f"Persona poll failed: {exc}") from exc
        if not (200 <= getattr(resp, "status_code", 500) < 300):
            raise KYCError(
                f"Persona poll returned {getattr(resp, 'status_code', 'unknown')}"
            )
        body = resp.json() if hasattr(resp, "json") else {}
        attrs = (body.get("data", {}) or {}).get("attributes", {}) or {}
        subject = attrs.get("reference-id") or ""
        return KYCVerdict(
            subject=str(subject),
            status=_map_persona_status(str(attrs.get("status", ""))),
            tier=KYCTier.STANDARD,
            inquiry_id=inquiry_id,
            reason=str(attrs.get("decision-reason") or ""),
            verified_at=self._clock(),
        )

    def revoke(self, *, subject: str, reason: str) -> None:
        # Persona doesn't expose a "revoke" API; revocation is a
        # platform-side state, not a provider-side one. This is a no-op
        # at the adapter; KYCService.revoke is what callers should hit.
        return None


def _map_persona_status(s: str) -> KYCStatus:
    s = s.lower()
    if s in ("approved", "completed", "passed"):
        return KYCStatus.APPROVED
    if s in ("declined", "failed", "rejected"):
        return KYCStatus.REJECTED
    if s in ("needs_review", "review", "manual_review"):
        return KYCStatus.REQUIRES_REVIEW
    if s in ("expired",):
        return KYCStatus.EXPIRED
    return KYCStatus.PENDING


# ---------------------------------------------------------------------------
# KYCService — the platform-side cache + audit + gate
# ---------------------------------------------------------------------------
@dataclass
class KYCService:
    """Caches verdicts, ages them out at ``retention_seconds``, exposes
    the gate the supervisor calls before a fund/spawn/withdrawal.

    The cache is in-memory in Phase 2 with a Postgres-backed variant
    landing in Phase 3 (per `docs/engineering/22` §"KYC integration").
    Audit recording is via the operator-audit log; the supervisor
    injects the recorder via :meth:`bind_audit_recorder`."""

    provider: KYCProvider
    retention_seconds: float = 365.0 * 86400.0  # 1 year default
    clock: Callable[[], float] = field(default=time.time)
    _verdicts: dict[str, KYCVerdict] = field(default_factory=dict, init=False)
    _audit_recorder: (
        Callable[[str, str, dict[str, Any]], None] | None
    ) = field(default=None, init=False)

    def bind_audit_recorder(
        self,
        recorder: Callable[[str, str, dict[str, Any]], None],
    ) -> None:
        """Recorder signature: ``(actor, action, metadata)``. The
        supervisor binds its own ``OperatorAuditLog.record`` partial."""
        self._audit_recorder = recorder

    def start_inquiry(
        self, *, subject: str, tier: KYCTier = KYCTier.STANDARD,
        actor: str = "platform",
    ) -> dict[str, Any]:
        try:
            out = self.provider.start_inquiry(subject=subject, tier=tier)
        except KYCError as exc:
            self._audit("kyc_start_failed", actor, {"subject": subject, "reason": str(exc)})
            raise
        self._audit("kyc_started", actor, {
            "subject": subject, "inquiry_id": out.get("inquiry_id"),
            "tier": tier.value,
        })
        return out

    def refresh_status(
        self, *, subject: str, inquiry_id: str, actor: str = "platform",
    ) -> KYCVerdict:
        """Poll the provider, cache the verdict, record the transition."""
        verdict = self.provider.poll_inquiry(inquiry_id)
        # Apply retention (only meaningful when verdict is APPROVED).
        if verdict.status == KYCStatus.APPROVED and verdict.expires_at is None:
            verdict = KYCVerdict(
                subject=verdict.subject, status=verdict.status,
                tier=verdict.tier, inquiry_id=verdict.inquiry_id,
                reason=verdict.reason,
                verified_at=verdict.verified_at or self.clock(),
                expires_at=(verdict.verified_at or self.clock()) + self.retention_seconds,
            )
        previous = self._verdicts.get(subject)
        self._verdicts[subject] = verdict
        if previous is None or previous.status != verdict.status:
            self._audit("kyc_status_changed", actor, {
                "subject": subject,
                "from": previous.status.value if previous else None,
                "to": verdict.status.value,
                "inquiry_id": inquiry_id,
            })
        return verdict

    def status_for(self, subject: str) -> KYCVerdict:
        """Cache-only read. Returns UNKNOWN when never inquired."""
        v = self._verdicts.get(subject)
        if v is None:
            return KYCVerdict(subject=subject, status=KYCStatus.UNKNOWN)
        # Apply expiry on read so callers always see the gated truth.
        if (
            v.status == KYCStatus.APPROVED
            and v.expires_at is not None
            and self.clock() > v.expires_at
        ):
            return KYCVerdict(
                subject=subject, status=KYCStatus.EXPIRED,
                tier=v.tier, inquiry_id=v.inquiry_id,
                reason="retention TTL exceeded",
                verified_at=v.verified_at,
                expires_at=v.expires_at,
            )
        return v

    def require_allowed(self, subject: str, *, action: str = "fund") -> KYCVerdict:
        """Gate entry: raises :class:`KYCError` unless the subject is
        currently APPROVED. The supervisor wraps fund/spawn/withdraw
        calls with this so a non-KYC'd user cannot move money."""
        v = self.status_for(subject)
        if not v.is_allowed():
            raise KYCError(
                f"KYC gate refused {action!r} for {subject!r}: "
                f"status={v.status.value} (reason: {v.reason or 'not approved'})"
            )
        return v

    def revoke(self, *, subject: str, reason: str, actor: str = "platform") -> None:
        """Forced revocation (suspected fraud, sanctions hit, owner request)."""
        try:
            self.provider.revoke(subject=subject, reason=reason)
        except Exception:
            # Provider-side revocation is best-effort; the platform cache
            # is the authoritative gate, and we mutate it unconditionally.
            pass
        existing = self._verdicts.get(subject)
        self._verdicts[subject] = KYCVerdict(
            subject=subject, status=KYCStatus.REJECTED,
            tier=existing.tier if existing else KYCTier.STANDARD,
            inquiry_id=existing.inquiry_id if existing else "",
            reason=f"revoked: {reason}",
            verified_at=self.clock(),
        )
        self._audit("kyc_revoked", actor, {"subject": subject, "reason": reason})

    def all_verdicts(self) -> list[KYCVerdict]:
        return list(self._verdicts.values())

    def _audit(self, action: str, actor: str, metadata: dict[str, Any]) -> None:
        if self._audit_recorder is None:
            return
        try:
            self._audit_recorder(actor, action, metadata)
        except Exception:
            # Audit recording must never destabilise the KYC service.
            pass
