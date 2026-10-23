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

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, runtime_checkable


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup. Starlette lower-cases header keys
    when a request's headers are converted to a plain dict, but a caller
    might pass the canonical casing — tolerate both."""
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return ""


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


_KYC_STATUS_VALUES = frozenset(s.value for s in KYCStatus)


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
    # Age gate (doc 05 minors-protection). The provider verifies date of
    # birth as part of identity; ``is_adult`` is its 18+ verdict. Defaults
    # True (an APPROVED identity implies adult unless the provider flags
    # otherwise); money-moving actions pass ``require_adult=True`` so a
    # provider that reports a minor — or a tier that didn't check age — is
    # refused.
    is_adult: bool = True

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
            "is_adult": self.is_adult,
        }


def _verdict_from_json(d: dict[str, Any]) -> KYCVerdict:
    return KYCVerdict(
        subject=d["subject"], status=KYCStatus(d["status"]),
        tier=KYCTier(d.get("tier", KYCTier.STANDARD.value)),
        inquiry_id=d.get("inquiry_id", ""), reason=d.get("reason", ""),
        verified_at=float(d.get("verified_at") or 0.0),
        expires_at=d.get("expires_at"), is_adult=bool(d.get("is_adult", True)),
    )


@dataclass(frozen=True)
class KYCWebhookEvent:
    """A provider webhook callback, after its signature has been verified.

    The platform re-polls the provider for the authoritative verdict rather
    than trusting the webhook body's status outright; ``status`` here is the
    parsed hint (used only when a re-poll isn't possible). ``subject`` may be
    empty — Onfido carries no caller reference on the run, so the service
    resolves the subject by polling. ``inquiry_id`` is the
    inquiry/workflow-run id the event is about."""

    provider: str
    event: str
    inquiry_id: str
    subject: str = ""
    status: KYCStatus | None = None


@runtime_checkable
class KYCProvider(Protocol):
    """Adapter surface the platform talks to. Real impls hit Persona /
    Onfido / Sumsub. ``start_inquiry`` returns an inquiry id +
    redirect URL the dashboard uses; ``poll_inquiry`` is the
    webhook-or-poll consumption side; ``verify_webhook`` authenticates a
    provider callback (raising :class:`KYCError` on a bad signature) and
    parses it into a :class:`KYCWebhookEvent`; ``revoke`` lets the platform
    short-circuit an approved subject (e.g. on suspected fraud)."""

    def start_inquiry(self, *, subject: str, tier: KYCTier = KYCTier.STANDARD) -> dict[str, Any]:
        """Returns ``{"inquiry_id": str, "redirect_url": str, "status": str}``."""
        ...

    def poll_inquiry(self, inquiry_id: str) -> KYCVerdict: ...

    def verify_webhook(
        self, *, raw_body: bytes, headers: Mapping[str, str]
    ) -> KYCWebhookEvent: ...

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
        webhook_secret: str = "",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._default_status = default_status
        self._default_tier = default_tier
        self._webhook_secret = webhook_secret
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
            is_adult=base.is_adult,
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

    def verify_webhook(
        self, *, raw_body: bytes, headers: Mapping[str, str]
    ) -> KYCWebhookEvent:
        """Lenient test/sim webhook: when a ``webhook_secret`` is configured,
        verify a hex HMAC-SHA256 of the raw body in the ``X-Kyc-Signature``
        header (constant-time); otherwise accept. The body is a flat
        ``{"inquiry_id", "subject", "status"}`` JSON object."""
        if self._webhook_secret:
            want = hmac.new(
                self._webhook_secret.encode("utf-8"), raw_body, hashlib.sha256,
            ).hexdigest()
            got = _header(headers, "X-Kyc-Signature")
            if not (got and hmac.compare_digest(got, want)):
                raise KYCError("stub webhook signature mismatch")
        try:
            body = json.loads(raw_body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise KYCError(f"stub webhook body not JSON: {exc}") from exc
        status_raw = str(body.get("status") or "")
        return KYCWebhookEvent(
            provider="stub",
            event=str(body.get("event") or "inquiry.updated"),
            inquiry_id=str(body.get("inquiry_id") or ""),
            subject=str(body.get("subject") or ""),
            status=KYCStatus(status_raw) if status_raw in _KYC_STATUS_VALUES else None,
        )

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
        webhook_secret: str = "",
        webhook_tolerance_seconds: float = 300.0,
        client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not api_key:
            raise KYCError("PersonaKYCProvider requires api_key")
        if not template_id:
            raise KYCError("PersonaKYCProvider requires template_id")
        self._webhook_secret = webhook_secret
        self._webhook_tolerance = float(webhook_tolerance_seconds)
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

    def verify_webhook(
        self, *, raw_body: bytes, headers: Mapping[str, str]
    ) -> KYCWebhookEvent:
        """Verify Persona's ``Persona-Signature`` header and parse the event.

        The header is comma-separated ``t=<unix>,v1=<hex>``; the signed string
        is ``f"{t}.{raw_body}"`` HMAC-SHA256'd with the webhook secret, hex
        digest. During secret rotation Persona sends multiple space-separated
        sets, so we accept a match against ANY ``v1`` present. The timestamp is
        checked for freshness to blunt replay. Raises :class:`KYCError` on a
        missing secret, missing/garbled header, stale timestamp, or no match.

        The inquiry lives at ``data.attributes.payload.data``; its
        ``reference-id`` is our subject and ``status`` the provider verdict."""
        if not self._webhook_secret:
            raise KYCError("Persona webhook secret not configured")
        header = _header(headers, "Persona-Signature")
        if not header:
            raise KYCError("missing Persona-Signature header")
        # Rotation: space-separated sets, each "t=..,v1=..".
        timestamp = ""
        signatures: list[str] = []
        for chunk in header.split():
            for pair in chunk.split(","):
                key, _, value = pair.partition("=")
                key = key.strip()
                if key == "t" and not timestamp:
                    timestamp = value.strip()
                elif key == "v1":
                    signatures.append(value.strip())
        if not timestamp or not signatures:
            raise KYCError("malformed Persona-Signature header")
        try:
            ts = float(timestamp)
        except ValueError as exc:
            raise KYCError("Persona-Signature timestamp not numeric") from exc
        if self._webhook_tolerance > 0 and abs(self._clock() - ts) > self._webhook_tolerance:
            raise KYCError("Persona webhook timestamp outside tolerance")
        signed = f"{timestamp}.".encode("utf-8") + raw_body
        want = hmac.new(
            self._webhook_secret.encode("utf-8"), signed, hashlib.sha256,
        ).hexdigest()
        if not any(hmac.compare_digest(sig, want) for sig in signatures):
            raise KYCError("Persona webhook signature mismatch")
        try:
            body = json.loads(raw_body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise KYCError(f"Persona webhook body not JSON: {exc}") from exc
        ev_attrs = (body.get("data", {}) or {}).get("attributes", {}) or {}
        inquiry = ((ev_attrs.get("payload", {}) or {}).get("data", {})) or {}
        inq_attrs = inquiry.get("attributes", {}) or {}
        return KYCWebhookEvent(
            provider="persona",
            event=str(ev_attrs.get("name") or ""),
            inquiry_id=str(inquiry.get("id") or ""),
            subject=str(inq_attrs.get("reference-id") or ""),
            status=_map_persona_status(str(inq_attrs.get("status") or "")),
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
# Onfido provider skeleton
# ---------------------------------------------------------------------------
_ONFIDO_REGIONS = {
    "eu": "https://api.onfido.com/v3.6",
    "us": "https://api.us.onfido.com/v3.6",
    "ca": "https://api.ca.onfido.com/v3.6",
}


class OnfidoKYCProvider:
    """Onfido Studio-workflow API adapter skeleton — the second KYC
    provider alongside :class:`PersonaKYCProvider`, behind the same
    :class:`KYCProvider` Protocol (doc 22 evaluated Persona *or* Onfido).

    ``start_inquiry`` creates an Onfido applicant and a workflow run;
    ``poll_inquiry`` reads the run's status. Like the Persona adapter this
    is an HTTP skeleton — coverage is via an injected stub transport
    returning canned JSON; production wires a real ``httpx.Client``. The
    endpoint shapes track Onfido's v3.6 API.

    Onfido applicants carry no caller-supplied reference field, so the
    adapter keeps an in-memory ``workflow-run id → subject`` map for
    verdict attribution. KYC documents never pass through here.
    """

    def __init__(
        self,
        *,
        api_token: str,
        workflow_id: str,
        region: str = "eu",
        webhook_token: str = "",
        client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not api_token:
            raise KYCError("OnfidoKYCProvider requires api_token")
        if not workflow_id:
            raise KYCError("OnfidoKYCProvider requires workflow_id")
        self._webhook_token = webhook_token
        base_url = _ONFIDO_REGIONS.get(region.lower())
        if base_url is None:
            raise KYCError(
                f"unknown Onfido region {region!r}; expected one of "
                f"{sorted(_ONFIDO_REGIONS)}"
            )
        if client is None:
            try:
                import httpx  # type: ignore[import]
            except ImportError as exc:
                raise KYCError(
                    "OnfidoKYCProvider requires httpx (or an injected "
                    "client): `pip install httpx`"
                ) from exc
            client = httpx.Client(
                base_url=base_url,
                headers={
                    "Authorization": f"Token token={api_token}",
                    "Accept": "application/json",
                },
                timeout=20.0,
            )
        self._client = client
        self._workflow_id = workflow_id
        self._clock = clock
        self._subjects: dict[str, str] = {}

    def start_inquiry(
        self, *, subject: str, tier: KYCTier = KYCTier.STANDARD
    ) -> dict[str, Any]:
        # 1. Create the applicant — identity data is collected later by the
        #    Onfido SDK / hosted flow, so the create body is minimal.
        try:
            applicant = self._client.post("/applicants", json={})
        except Exception as exc:
            raise KYCError(f"Onfido create-applicant failed: {exc}") from exc
        if not (200 <= getattr(applicant, "status_code", 500) < 300):
            raise KYCError(
                f"Onfido create-applicant returned "
                f"{getattr(applicant, 'status_code', 'unknown')}"
            )
        a_body = applicant.json() if hasattr(applicant, "json") else {}
        applicant_id = str(a_body.get("id") or "")
        if not applicant_id:
            raise KYCError("Onfido returned no applicant id")
        # 2. Launch the Studio workflow run for that applicant.
        try:
            run = self._client.post(
                "/workflow_runs",
                json={
                    "applicant_id": applicant_id,
                    "workflow_id": self._workflow_id,
                    "tags": [f"tier:{tier.value}"],
                },
            )
        except Exception as exc:
            raise KYCError(
                f"Onfido create-workflow-run failed: {exc}"
            ) from exc
        if not (200 <= getattr(run, "status_code", 500) < 300):
            raise KYCError(
                f"Onfido create-workflow-run returned "
                f"{getattr(run, 'status_code', 'unknown')}"
            )
        body = run.json() if hasattr(run, "json") else {}
        run_id = str(body.get("id") or "")
        if not run_id:
            raise KYCError("Onfido returned no workflow-run id")
        self._subjects[run_id] = subject
        link = body.get("link") or {}
        return {
            "inquiry_id": run_id,
            "redirect_url": (
                (link.get("url") if isinstance(link, dict) else "")
                or body.get("dashboard_url")
                or ""
            ),
            "status": _map_onfido_status(str(body.get("status", ""))).value,
        }

    def poll_inquiry(self, inquiry_id: str) -> KYCVerdict:
        try:
            resp = self._client.get(f"/workflow_runs/{inquiry_id}")
        except Exception as exc:
            raise KYCError(f"Onfido poll failed: {exc}") from exc
        if not (200 <= getattr(resp, "status_code", 500) < 300):
            raise KYCError(
                f"Onfido poll returned "
                f"{getattr(resp, 'status_code', 'unknown')}"
            )
        body = resp.json() if hasattr(resp, "json") else {}
        return KYCVerdict(
            subject=self._subjects.get(inquiry_id, ""),
            status=_map_onfido_status(str(body.get("status", ""))),
            tier=KYCTier.STANDARD,
            inquiry_id=inquiry_id,
            reason=str(body.get("reason") or body.get("status") or ""),
            verified_at=self._clock(),
        )

    def verify_webhook(
        self, *, raw_body: bytes, headers: Mapping[str, str]
    ) -> KYCWebhookEvent:
        """Verify Onfido's ``X-SHA2-Signature`` and parse the event.

        The signature is HMAC-SHA256 of the RAW request body keyed by the
        webhook's token (hex), constant-time compared. The event lives at
        ``payload``: ``action`` (e.g. ``workflow_run.completed``),
        ``object.id`` (the workflow-run id), and ``object.status``. Onfido
        carries no caller reference on the run, so ``subject`` is resolved
        from the adapter's in-memory ``run id → subject`` map (and, failing
        that, by the service re-polling). Raises :class:`KYCError` on a
        missing token, missing header, or signature mismatch."""
        if not self._webhook_token:
            raise KYCError("Onfido webhook token not configured")
        got = _header(headers, "X-SHA2-Signature")
        if not got:
            raise KYCError("missing X-SHA2-Signature header")
        want = hmac.new(
            self._webhook_token.encode("utf-8"), raw_body, hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(got, want):
            raise KYCError("Onfido webhook signature mismatch")
        try:
            body = json.loads(raw_body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise KYCError(f"Onfido webhook body not JSON: {exc}") from exc
        payload = body.get("payload", {}) or {}
        obj = payload.get("object", {}) or {}
        run_id = str(obj.get("id") or "")
        return KYCWebhookEvent(
            provider="onfido",
            event=str(payload.get("action") or ""),
            inquiry_id=run_id,
            subject=self._subjects.get(run_id, ""),
            status=_map_onfido_status(str(obj.get("status") or "")),
        )

    def revoke(self, *, subject: str, reason: str) -> None:
        # Onfido has no revoke API — revocation is platform-side state, the
        # same as the Persona adapter. KYCService.revoke is authoritative.
        return None


def _map_onfido_status(s: str) -> KYCStatus:
    s = s.lower()
    if s in ("approved", "clear", "complete", "completed", "passed"):
        return KYCStatus.APPROVED
    if s in ("declined", "rejected", "failed", "fail", "abandoned"):
        return KYCStatus.REJECTED
    if s in ("review", "awaiting_review", "manual_review"):
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
    # inquiry_id → the subject who started it. The authority on whose verdict
    # an inquiry is: nobody can attach another person's approval to themselves.
    _inquiries: dict[str, str] = field(default_factory=dict, init=False)
    _store: Any = field(default=None, init=False)
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

    def attach_store(self, store: Any) -> None:
        """Persist verdicts + inquiry bindings (``save_verdict``,
        ``bind_inquiry``, ``load``), so a restart doesn't turn every
        verified user back into UNKNOWN."""
        self._store = store
        verdicts, inquiries = store.load()
        self._verdicts.update(
            {s: _verdict_from_json(v) for s, v in verdicts.items()}
        )
        self._inquiries.update(inquiries)

    def _remember(self, subject: str, verdict: KYCVerdict) -> None:
        self._verdicts[subject] = verdict
        if self._store is not None:
            self._store.save_verdict(subject, verdict.to_json())

    def start_inquiry(
        self, *, subject: str, tier: KYCTier = KYCTier.STANDARD,
        actor: str = "platform",
    ) -> dict[str, Any]:
        try:
            out = self.provider.start_inquiry(subject=subject, tier=tier)
        except KYCError as exc:
            self._audit("kyc_start_failed", actor, {"subject": subject, "reason": str(exc)})
            raise
        inquiry_id = str(out.get("inquiry_id") or "")
        if inquiry_id:
            self._inquiries[inquiry_id] = subject
            if self._store is not None:
                self._store.bind_inquiry(inquiry_id, subject)
        self._audit("kyc_started", actor, {
            "subject": subject, "inquiry_id": out.get("inquiry_id"),
            "tier": tier.value,
        })
        return out

    def refresh_status(
        self, *, subject: str, inquiry_id: str, actor: str = "platform",
    ) -> KYCVerdict:
        """Poll the provider, cache the verdict, record the transition.

        The verdict only ever lands on the subject who started the inquiry:
        an unknown inquiry, or one started by (or attributed by the provider
        to) someone else, is refused — so an approved inquiry cannot unlock
        another account."""
        owner = self._inquiries.get(inquiry_id)
        if owner is None:
            raise KYCError(f"unknown KYC inquiry {inquiry_id!r}")
        if subject != owner:
            raise KYCError(f"KYC inquiry {inquiry_id!r} belongs to another subject")
        verdict = self.provider.poll_inquiry(inquiry_id)
        if verdict.subject and verdict.subject != owner:
            raise KYCError(
                f"provider attributes inquiry {inquiry_id!r} to another subject"
            )
        # Apply retention (only meaningful when verdict is APPROVED).
        if verdict.status == KYCStatus.APPROVED and verdict.expires_at is None:
            verdict = KYCVerdict(
                subject=verdict.subject, status=verdict.status,
                tier=verdict.tier, inquiry_id=verdict.inquiry_id,
                reason=verdict.reason,
                verified_at=verdict.verified_at or self.clock(),
                expires_at=(verdict.verified_at or self.clock()) + self.retention_seconds,
                is_adult=verdict.is_adult,
            )
        previous = self._verdicts.get(subject)
        self._remember(subject, verdict)
        if previous is None or previous.status != verdict.status:
            self._audit("kyc_status_changed", actor, {
                "subject": subject,
                "from": previous.status.value if previous else None,
                "to": verdict.status.value,
                "inquiry_id": inquiry_id,
            })
        return verdict

    def handle_webhook(
        self, *, raw_body: bytes, headers: Mapping[str, str],
        actor: str = "platform.kyc_webhook",
    ) -> KYCVerdict:
        """Authenticate a provider callback and cache the resulting verdict.

        Delegates signature verification + parsing to the configured
        provider (:meth:`KYCProvider.verify_webhook`, which raises
        :class:`KYCError` on a bad signature), then re-polls for the
        authoritative verdict via :meth:`ingest_webhook`. This is the
        provider-driven counterpart to the operator/poll path
        :meth:`refresh_status`."""
        event = self.provider.verify_webhook(raw_body=raw_body, headers=headers)
        return self.ingest_webhook(event, actor=actor)

    def ingest_webhook(
        self, event: KYCWebhookEvent, *, actor: str = "platform.kyc_webhook",
    ) -> KYCVerdict:
        """Cache the verdict for a verified webhook event. Re-polls the
        provider for the authoritative status (the webhook only authenticates
        and identifies *which* inquiry changed). The subject comes from the
        event when present (Persona's reference-id) or from the poll
        (Onfido attributes the run server-side)."""
        if not event.inquiry_id:
            raise KYCError("KYC webhook event has no inquiry id")
        # Our own binding first (survives restarts); then the event / a poll.
        subject = self._inquiries.get(event.inquiry_id) or event.subject
        if not subject:
            # Onfido: discover the subject by polling, then cache under it.
            polled = self.provider.poll_inquiry(event.inquiry_id)
            subject = polled.subject
        if not subject:
            raise KYCError(
                f"KYC webhook for inquiry {event.inquiry_id!r} has no "
                "resolvable subject"
            )
        return self.refresh_status(
            subject=subject, inquiry_id=event.inquiry_id, actor=actor,
        )

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

    def require_allowed(
        self, subject: str, *, action: str = "fund", require_adult: bool = False,
    ) -> KYCVerdict:
        """Gate entry: raises :class:`KYCError` unless the subject is
        currently APPROVED. The supervisor wraps fund/spawn/withdraw
        calls with this so a non-KYC'd user cannot move money.

        When ``require_adult`` is set (the money-moving + spawn paths), an
        APPROVED-but-not-adult verdict is also refused (doc 05 minors
        protection)."""
        v = self.status_for(subject)
        if not v.is_allowed():
            raise KYCError(
                f"KYC gate refused {action!r} for {subject!r}: "
                f"status={v.status.value} (reason: {v.reason or 'not approved'})"
            )
        if require_adult and not v.is_adult:
            raise KYCError(
                f"KYC gate refused {action!r} for {subject!r}: "
                f"age verification failed (must be 18+)"
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
        self._remember(subject, KYCVerdict(
            subject=subject, status=KYCStatus.REJECTED,
            tier=existing.tier if existing else KYCTier.STANDARD,
            inquiry_id=existing.inquiry_id if existing else "",
            reason=f"revoked: {reason}",
            verified_at=self.clock(),
        ))
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
