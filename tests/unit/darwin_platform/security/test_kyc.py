"""Tests for the KYC service + providers (docs/engineering/22).

Coverage:
- StubKYCProvider: default-allow, per-subject override drives reject + pending.
- PersonaKYCProvider: requires api_key + template; lazy httpx import; status mapping.
- KYCService: caches verdicts, applies retention TTL, gates require_allowed,
  audit recorder hook, revoke cascades to provider + cache.
"""

from __future__ import annotations

import types

import pytest

from darwin_platform.security.kyc import (
    KYCError,
    KYCService,
    KYCStatus,
    KYCTier,
    KYCVerdict,
    PersonaKYCProvider,
    StubKYCProvider,
)


# ---------------------------------------------------------------------------
# StubKYCProvider
# ---------------------------------------------------------------------------

def test_stub_default_approves():
    p = StubKYCProvider()
    out = p.start_inquiry(subject="alice", tier=KYCTier.STANDARD)
    v = p.poll_inquiry(out["inquiry_id"])
    assert v.status == KYCStatus.APPROVED
    assert v.subject == "alice"
    assert v.tier == KYCTier.STANDARD


def test_stub_override_drives_rejection():
    p = StubKYCProvider()
    p.set_override("bob", KYCVerdict(
        subject="bob", status=KYCStatus.REJECTED, reason="risk score 99",
    ))
    out = p.start_inquiry(subject="bob")
    v = p.poll_inquiry(out["inquiry_id"])
    assert v.status == KYCStatus.REJECTED
    assert v.reason == "risk score 99"


def test_stub_revoke_marks_all_inquiries_rejected():
    p = StubKYCProvider()
    a = p.start_inquiry(subject="carol")
    b = p.start_inquiry(subject="carol")
    p.revoke(subject="carol", reason="ofac hit")
    assert p.poll_inquiry(a["inquiry_id"]).status == KYCStatus.REJECTED
    assert p.poll_inquiry(b["inquiry_id"]).status == KYCStatus.REJECTED


def test_stub_unknown_inquiry_raises():
    p = StubKYCProvider()
    with pytest.raises(KYCError):
        p.poll_inquiry("inq-nonexistent")


# ---------------------------------------------------------------------------
# PersonaKYCProvider (with stub HTTP client)
# ---------------------------------------------------------------------------

class _StubHTTP:
    """Mimics httpx.Client.post/get returning canned envelopes."""

    def __init__(self, *, post_body=None, get_body=None, status=200):
        self.post_body = post_body or {"data": {
            "id": "inq-persona-1",
            "attributes": {"status": "pending",
                           "hosted-flow-url": "https://withpersona.com/x"},
        }}
        self.get_body = get_body or {"data": {
            "id": "inq-persona-1",
            "attributes": {"status": "approved", "reference-id": "alice",
                           "decision-reason": "auto-approve"},
        }}
        self.status = status
        self.posts: list = []
        self.gets: list = []

    def post(self, url, json=None):
        self.posts.append((url, json))
        return types.SimpleNamespace(status_code=self.status,
                                     json=lambda: self.post_body)

    def get(self, url):
        self.gets.append(url)
        return types.SimpleNamespace(status_code=self.status,
                                     json=lambda: self.get_body)


def test_persona_requires_api_key():
    with pytest.raises(KYCError, match="api_key"):
        PersonaKYCProvider(api_key="", template_id="t",
                           client=_StubHTTP())


def test_persona_requires_template_id():
    with pytest.raises(KYCError, match="template_id"):
        PersonaKYCProvider(api_key="k", template_id="",
                           client=_StubHTTP())


def test_persona_start_inquiry_round_trip():
    http = _StubHTTP()
    p = PersonaKYCProvider(api_key="key", template_id="tmpl-1",
                           client=http)
    out = p.start_inquiry(subject="alice", tier=KYCTier.ENHANCED)
    assert out["inquiry_id"] == "inq-persona-1"
    assert out["redirect_url"].startswith("https://withpersona.com")
    assert out["status"] == "pending"
    assert len(http.posts) == 1
    sent = http.posts[0][1]["data"]["attributes"]
    assert sent["inquiry_template_id"] == "tmpl-1"
    assert sent["reference_id"] == "alice"


def test_persona_poll_maps_approved():
    http = _StubHTTP()
    p = PersonaKYCProvider(api_key="key", template_id="tmpl-1",
                           client=http)
    v = p.poll_inquiry("inq-persona-1")
    assert v.status == KYCStatus.APPROVED
    assert v.subject == "alice"
    assert v.reason == "auto-approve"


def test_persona_poll_maps_review_status():
    http = _StubHTTP(get_body={"data": {
        "id": "inq-persona-1",
        "attributes": {"status": "needs_review", "reference-id": "alice"},
    }})
    p = PersonaKYCProvider(api_key="key", template_id="tmpl-1",
                           client=http)
    v = p.poll_inquiry("inq-persona-1")
    assert v.status == KYCStatus.REQUIRES_REVIEW


def test_persona_post_failure_surfaces_as_kyc_error():
    http = _StubHTTP(status=503)
    p = PersonaKYCProvider(api_key="key", template_id="tmpl-1",
                           client=http)
    with pytest.raises(KYCError, match="503"):
        p.start_inquiry(subject="alice")


# ---------------------------------------------------------------------------
# KYCService
# ---------------------------------------------------------------------------

def _clock_with_advance(start: float = 1_700_000_000.0):
    t = [start]

    def now() -> float:
        return t[0]

    def advance(s: float) -> None:
        t[0] += float(s)

    now.advance = advance  # type: ignore[attr-defined]
    return now


def test_service_caches_and_changes_status():
    provider = StubKYCProvider()
    svc = KYCService(provider=provider)
    out = svc.start_inquiry(subject="alice")
    v1 = svc.refresh_status(subject="alice", inquiry_id=out["inquiry_id"])
    assert v1.status == KYCStatus.APPROVED
    # The cache now carries the verdict; status_for is a cheap read.
    cached = svc.status_for("alice")
    assert cached.status == KYCStatus.APPROVED
    # Status for an unknown subject defaults to UNKNOWN.
    assert svc.status_for("dave").status == KYCStatus.UNKNOWN


def test_service_require_allowed_refuses_non_approved():
    provider = StubKYCProvider(default_status=KYCStatus.REJECTED)
    svc = KYCService(provider=provider)
    out = svc.start_inquiry(subject="bob")
    svc.refresh_status(subject="bob", inquiry_id=out["inquiry_id"])
    with pytest.raises(KYCError, match="KYC gate refused"):
        svc.require_allowed("bob", action="deposit")


def test_service_expires_approved_after_retention():
    clock = _clock_with_advance()
    provider = StubKYCProvider(clock=clock)
    svc = KYCService(provider=provider, retention_seconds=60.0, clock=clock)
    out = svc.start_inquiry(subject="carol")
    svc.refresh_status(subject="carol", inquiry_id=out["inquiry_id"])
    assert svc.status_for("carol").status == KYCStatus.APPROVED
    clock.advance(120.0)  # past retention
    assert svc.status_for("carol").status == KYCStatus.EXPIRED


def test_service_revoke_flips_to_rejected_in_cache():
    provider = StubKYCProvider()
    svc = KYCService(provider=provider)
    out = svc.start_inquiry(subject="eve")
    svc.refresh_status(subject="eve", inquiry_id=out["inquiry_id"])
    svc.revoke(subject="eve", reason="fraud signal")
    v = svc.status_for("eve")
    assert v.status == KYCStatus.REJECTED
    assert "fraud signal" in v.reason


def test_service_audit_recorder_captures_lifecycle():
    provider = StubKYCProvider()
    recorded: list[tuple[str, str, dict]] = []
    svc = KYCService(provider=provider)
    svc.bind_audit_recorder(
        lambda actor, action, meta: recorded.append((actor, action, meta))
    )
    out = svc.start_inquiry(subject="frank", actor="api.session")
    svc.refresh_status(
        subject="frank", inquiry_id=out["inquiry_id"], actor="api.session",
    )
    actions = [r[1] for r in recorded]
    assert "kyc_started" in actions
    assert "kyc_status_changed" in actions
