"""Tests for the KYC service + providers (docs/engineering/22).

Coverage:
- StubKYCProvider: default-allow, per-subject override drives reject + pending.
- PersonaKYCProvider: requires api_key + template; lazy httpx import; status mapping.
- KYCService: caches verdicts, applies retention TTL, gates require_allowed,
  audit recorder hook, revoke cascades to provider + cache.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import types

import pytest

from darwin_platform.security.kyc import (
    KYCError,
    KYCProvider,
    KYCService,
    KYCStatus,
    KYCTier,
    KYCVerdict,
    KYCWebhookEvent,
    OnfidoKYCProvider,
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
# OnfidoKYCProvider (with stub HTTP client)
# ---------------------------------------------------------------------------

class _OnfidoStubHTTP:
    """Mimics the Onfido v3.6 endpoints the adapter calls — applicant
    create, workflow-run create, workflow-run poll."""

    def __init__(
        self, *, run_status="processing", run_get_status="approved",
        status=200,
    ):
        self.status = status
        self.run_status = run_status
        self.run_get_status = run_get_status
        self.posts: list = []
        self.gets: list = []

    def post(self, url, json=None):
        self.posts.append((url, json))
        if url == "/applicants":
            body: dict = {"id": "applicant-1"}
        elif url == "/workflow_runs":
            body = {
                "id": "wfr-1", "status": self.run_status,
                "link": {"url": "https://onfido.app/wfr-1"},
            }
        else:
            body = {}
        return types.SimpleNamespace(
            status_code=self.status, json=lambda: body,
        )

    def get(self, url):
        self.gets.append(url)
        body = {"id": "wfr-1", "status": self.run_get_status,
                "reason": "auto"}
        return types.SimpleNamespace(
            status_code=self.status, json=lambda: body,
        )


def test_onfido_requires_api_token():
    with pytest.raises(KYCError, match="api_token"):
        OnfidoKYCProvider(
            api_token="", workflow_id="wf", client=_OnfidoStubHTTP(),
        )


def test_onfido_requires_workflow_id():
    with pytest.raises(KYCError, match="workflow_id"):
        OnfidoKYCProvider(
            api_token="t", workflow_id="", client=_OnfidoStubHTTP(),
        )


def test_onfido_rejects_unknown_region():
    with pytest.raises(KYCError, match="region"):
        OnfidoKYCProvider(
            api_token="t", workflow_id="wf", region="antarctica",
            client=_OnfidoStubHTTP(),
        )


def test_onfido_satisfies_the_kyc_provider_protocol():
    p = OnfidoKYCProvider(
        api_token="t", workflow_id="wf", client=_OnfidoStubHTTP(),
    )
    assert isinstance(p, KYCProvider)


def test_onfido_start_inquiry_creates_applicant_then_workflow_run():
    http = _OnfidoStubHTTP()
    p = OnfidoKYCProvider(api_token="t", workflow_id="wf-99", client=http)
    out = p.start_inquiry(subject="alice", tier=KYCTier.ENHANCED)
    assert out["inquiry_id"] == "wfr-1"
    assert out["redirect_url"] == "https://onfido.app/wfr-1"
    assert [u for u, _ in http.posts] == ["/applicants", "/workflow_runs"]
    wf_body = http.posts[1][1]
    assert wf_body["applicant_id"] == "applicant-1"
    assert wf_body["workflow_id"] == "wf-99"
    assert "tier:enhanced" in wf_body["tags"]


def test_onfido_poll_maps_approved_and_recovers_subject():
    http = _OnfidoStubHTTP(run_get_status="clear")
    p = OnfidoKYCProvider(api_token="t", workflow_id="wf", client=http)
    p.start_inquiry(subject="alice")
    v = p.poll_inquiry("wfr-1")
    assert v.status == KYCStatus.APPROVED
    assert v.subject == "alice"  # from the adapter's run→subject map


def test_onfido_poll_maps_declined_and_review():
    p1 = OnfidoKYCProvider(
        api_token="t", workflow_id="wf",
        client=_OnfidoStubHTTP(run_get_status="declined"),
    )
    p1.start_inquiry(subject="bob")
    assert p1.poll_inquiry("wfr-1").status == KYCStatus.REJECTED

    p2 = OnfidoKYCProvider(
        api_token="t", workflow_id="wf",
        client=_OnfidoStubHTTP(run_get_status="review"),
    )
    p2.start_inquiry(subject="cara")
    assert p2.poll_inquiry("wfr-1").status == KYCStatus.REQUIRES_REVIEW


def test_onfido_http_failure_surfaces_as_kyc_error():
    http = _OnfidoStubHTTP(status=503)
    p = OnfidoKYCProvider(api_token="t", workflow_id="wf", client=http)
    with pytest.raises(KYCError, match="503"):
        p.start_inquiry(subject="alice")


def test_onfido_revoke_is_a_noop():
    p = OnfidoKYCProvider(
        api_token="t", workflow_id="wf", client=_OnfidoStubHTTP(),
    )
    p.revoke(subject="alice", reason="x")  # no raise


def test_cli_parses_onfido_flags():
    from darwin_platform.__main__ import build_parser
    args = build_parser().parse_args([
        "serve", "--kyc-provider", "onfido",
        "--kyc-onfido-api-token", "tok",
        "--kyc-onfido-workflow-id", "wf-1",
        "--kyc-onfido-region", "us",
    ])
    assert args.kyc_provider == "onfido"
    assert args.kyc_onfido_api_token == "tok"
    assert args.kyc_onfido_region == "us"


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


# ---------------------------------------------------------------------------
# Signed webhook ingestion (Persona / Onfido / Stub)
# ---------------------------------------------------------------------------

_FIXED_CLOCK = 1_700_000_000.0


def _persona_event(*, inquiry_id="inq-1", subject="alice", status="approved"):
    return json.dumps({
        "data": {
            "type": "event",
            "id": "evt-1",
            "attributes": {
                "name": f"inquiry.{status}",
                "payload": {
                    "data": {
                        "type": "inquiry",
                        "id": inquiry_id,
                        "attributes": {
                            "status": status,
                            "reference-id": subject,
                        },
                    }
                },
            },
        }
    }).encode("utf-8")


def _persona_sig(secret: str, body: bytes, ts: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), f"{ts}.".encode("utf-8") + body,
        hashlib.sha256,
    ).hexdigest()
    return digest


def test_persona_verify_webhook_accepts_valid_signature():
    body = _persona_event(inquiry_id="inq-9", subject="alice", status="approved")
    ts = str(int(_FIXED_CLOCK))
    sig = _persona_sig("whsec", body, ts)
    p = PersonaKYCProvider(
        api_key="k", template_id="t", webhook_secret="whsec",
        client=_StubHTTP(), clock=lambda: _FIXED_CLOCK,
    )
    event = p.verify_webhook(
        raw_body=body, headers={"Persona-Signature": f"t={ts},v1={sig}"},
    )
    assert isinstance(event, KYCWebhookEvent)
    assert event.provider == "persona"
    assert event.inquiry_id == "inq-9"
    assert event.subject == "alice"
    assert event.status == KYCStatus.APPROVED
    assert event.event == "inquiry.approved"


def test_persona_verify_webhook_rejects_bad_signature():
    body = _persona_event()
    ts = str(int(_FIXED_CLOCK))
    p = PersonaKYCProvider(
        api_key="k", template_id="t", webhook_secret="whsec",
        client=_StubHTTP(), clock=lambda: _FIXED_CLOCK,
    )
    with pytest.raises(KYCError, match="signature mismatch"):
        p.verify_webhook(
            raw_body=body,
            headers={"Persona-Signature": f"t={ts},v1=deadbeef"},
        )


def test_persona_verify_webhook_rejects_stale_timestamp():
    body = _persona_event()
    stale_ts = str(int(_FIXED_CLOCK - 10_000))
    sig = _persona_sig("whsec", body, stale_ts)
    p = PersonaKYCProvider(
        api_key="k", template_id="t", webhook_secret="whsec",
        webhook_tolerance_seconds=300.0,
        client=_StubHTTP(), clock=lambda: _FIXED_CLOCK,
    )
    with pytest.raises(KYCError, match="tolerance"):
        p.verify_webhook(
            raw_body=body,
            headers={"Persona-Signature": f"t={stale_ts},v1={sig}"},
        )


def test_persona_verify_webhook_accepts_during_secret_rotation():
    # Persona sends space-separated sets while a secret is rotating; a match
    # against ANY v1 is sufficient.
    body = _persona_event(subject="rotato")
    ts = str(int(_FIXED_CLOCK))
    new_sig = _persona_sig("new-secret", body, ts)
    p = PersonaKYCProvider(
        api_key="k", template_id="t", webhook_secret="new-secret",
        client=_StubHTTP(), clock=lambda: _FIXED_CLOCK,
    )
    header = f"t={ts},v1=oldoldold t={ts},v1={new_sig}"
    event = p.verify_webhook(raw_body=body, headers={"Persona-Signature": header})
    assert event.subject == "rotato"


def test_persona_verify_webhook_requires_secret():
    p = PersonaKYCProvider(api_key="k", template_id="t", client=_StubHTTP())
    with pytest.raises(KYCError, match="secret not configured"):
        p.verify_webhook(raw_body=b"{}", headers={"Persona-Signature": "t=1,v1=x"})


def _onfido_event(*, run_id="wfr-1", action="workflow_run.completed",
                  status="approved"):
    return json.dumps({
        "payload": {
            "resource_type": "workflow_run",
            "action": action,
            "object": {
                "id": run_id,
                "status": status,
                "href": f"https://api.onfido.com/v3.6/workflow_runs/{run_id}",
            },
        }
    }).encode("utf-8")


def test_onfido_verify_webhook_accepts_and_resolves_subject():
    http = _OnfidoStubHTTP()
    p = OnfidoKYCProvider(
        api_token="t", workflow_id="wf", webhook_token="wht", client=http,
    )
    p.start_inquiry(subject="alice")  # populates run→subject map (wfr-1)
    body = _onfido_event(run_id="wfr-1", status="clear")
    sig = hmac.new(b"wht", body, hashlib.sha256).hexdigest()
    event = p.verify_webhook(
        raw_body=body, headers={"X-SHA2-Signature": sig},
    )
    assert event.provider == "onfido"
    assert event.inquiry_id == "wfr-1"
    assert event.subject == "alice"
    assert event.status == KYCStatus.APPROVED


def test_onfido_verify_webhook_rejects_bad_signature():
    p = OnfidoKYCProvider(
        api_token="t", workflow_id="wf", webhook_token="wht",
        client=_OnfidoStubHTTP(),
    )
    body = _onfido_event()
    with pytest.raises(KYCError, match="signature mismatch"):
        p.verify_webhook(raw_body=body, headers={"X-SHA2-Signature": "00"})


def test_stub_verify_webhook_roundtrip():
    # No secret -> accept and parse.
    p = StubKYCProvider()
    body = json.dumps(
        {"inquiry_id": "inq-1", "subject": "alice", "status": "approved"}
    ).encode("utf-8")
    event = p.verify_webhook(raw_body=body, headers={})
    assert event.inquiry_id == "inq-1"
    assert event.subject == "alice"
    assert event.status == KYCStatus.APPROVED
    # With a secret -> require the X-Kyc-Signature HMAC.
    ps = StubKYCProvider(webhook_secret="s")
    sig = hmac.new(b"s", body, hashlib.sha256).hexdigest()
    assert ps.verify_webhook(
        raw_body=body, headers={"X-Kyc-Signature": sig}
    ).subject == "alice"
    with pytest.raises(KYCError, match="signature mismatch"):
        ps.verify_webhook(raw_body=body, headers={"X-Kyc-Signature": "bad"})


def test_service_handle_webhook_caches_verdict():
    # End-to-end: the verified webhook refreshes + caches the verdict so the
    # gate sees APPROVED without a manual poll.
    provider = StubKYCProvider(webhook_secret="s")
    svc = KYCService(provider=provider)
    out = svc.start_inquiry(subject="alice")
    body = json.dumps(
        {"inquiry_id": out["inquiry_id"], "subject": "alice"}
    ).encode("utf-8")
    sig = hmac.new(b"s", body, hashlib.sha256).hexdigest()
    verdict = svc.handle_webhook(
        raw_body=body, headers={"X-Kyc-Signature": sig},
    )
    assert verdict.status == KYCStatus.APPROVED
    assert svc.status_for("alice").status == KYCStatus.APPROVED
    svc.require_allowed("alice", action="fund")  # no raise


def test_service_ingest_webhook_resolves_subject_via_poll():
    # An event with no subject (Onfido-shaped) is resolved by polling.
    provider = StubKYCProvider()
    svc = KYCService(provider=provider)
    out = svc.start_inquiry(subject="bob")
    event = KYCWebhookEvent(
        provider="stub", event="inquiry.updated",
        inquiry_id=out["inquiry_id"], subject="",
    )
    verdict = svc.ingest_webhook(event)
    assert verdict.subject == "bob"
    assert verdict.status == KYCStatus.APPROVED


def test_ingest_webhook_without_inquiry_id_raises():
    svc = KYCService(provider=StubKYCProvider())
    with pytest.raises(KYCError, match="no inquiry id"):
        svc.ingest_webhook(
            KYCWebhookEvent(provider="stub", event="x", inquiry_id="")
        )


def test_cli_parses_kyc_webhook_secret_flags():
    from darwin_platform.__main__ import build_parser
    args = build_parser().parse_args([
        "serve",
        "--kyc-provider", "persona",
        "--kyc-persona-webhook-secret", "whsec",
        "--kyc-onfido-webhook-token", "wht",
    ])
    assert args.kyc_persona_webhook_secret == "whsec"
    assert args.kyc_onfido_webhook_token == "wht"
