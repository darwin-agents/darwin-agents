"""compliance.kyc_submitter — the sensitive-read neuron that closes the
escalation → owner grant → sensitive read loop (doc 16; audit #30).

These drive the REAL TieredVault + MemoryOwnerGrantStore so the grant is
genuinely consumed (single-use) on a SENSITIVE read. The load-bearing
properties: (1) the first pass escalates with a kyc ticket and reads nothing;
(2) a resume with a minted grant performs the read once; (3) the document value
never appears in the result output or the audit log.
"""

from __future__ import annotations

import asyncio

from darwin_dna.machinery.audit_logger import AuditLogger
from darwin_dna.machinery.vault import ScopedVaultView, TieredVault, VaultTier
from darwin_dna.neurons._base import NeuronContext, Task
from darwin_dna.neurons.compliance_kyc import ComplianceKycSubmitter
from darwin_fakes.fake_platform import MemoryAuditSink
from darwin_fakes.fake_vault import FakeVault, MemoryOwnerGrantStore

_TAGS = frozenset({"compliance.submit", "web.operate"})
_DOC = "PASSPORT-PII-SECRET"


class _Resp:
    def __init__(self, status: int = 200) -> None:
        self.status_code = status


class _Http:
    def __init__(self, status: int = 200) -> None:
        self.calls: list[dict] = []
        self._status = status

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        return _Resp(self._status)


def _harness(*, grant: str | None = None, write_doc: bool = True,
             http: _Http | None = None):
    raw = FakeVault()
    grants = MemoryOwnerGrantStore()
    tiered = TieredVault(raw, darwin_id="d-1", grants=grants)
    if write_doc:
        tiered.put(VaultTier.SENSITIVE, "passport", _DOC)
    if grant is not None:
        grants.grant(grant)
    view = ScopedVaultView(tiered, capability_tags=_TAGS,
                           default_scope="compliance.submit")
    sink = MemoryAuditSink()
    ctx = NeuronContext(
        darwin_id="d-1", llm=None, audit=AuditLogger("d-1", sink),
        vault=view, http=http,
    )
    return ctx, grants, sink


def _run(neuron, payload, ctx):
    task = Task(id="kyc-1", kind="compliance.submit",
                required_tags=_TAGS, payload=payload)
    plan = asyncio.run(neuron.plan(task, ctx))
    return asyncio.run(neuron.execute(plan, ctx))


# -- first pass: escalate, touch nothing ----------------------------------
def test_first_pass_escalates_kyc_and_reads_nothing():
    n = ComplianceKycSubmitter()
    ctx, grants, sink = _harness(grant=None)
    res = _run(n, {"document_key": "passport",
                   "endpoint": "https://kyc.example/submit"}, ctx)
    assert res.escalated is True
    # error_code="kyc" is what the dispatch node classifies into the KYC ticket
    # class → the supervisor mints the one-time grant on approval.
    assert res.error_code == "kyc"
    assert res.output.get("ticket")
    # No sensitive read happened, so no grant was touched and no doc leaked.
    assert _DOC not in repr(sink.read_all())


# -- resume pass: read once, submit, never leak ---------------------------
def test_resume_reads_sensitive_then_submits_without_leaking():
    n = ComplianceKycSubmitter()
    http = _Http(200)
    ctx, grants, sink = _harness(grant="grant-abc", http=http)
    res = _run(n, {"document_key": "passport",
                   "endpoint": "https://kyc.example/submit",
                   "grant_token": "grant-abc"}, ctx)
    assert res.ok is True
    assert res.output == {"submitted": True, "document_key": "passport",
                          "status": 200}
    # The document was submitted to the provider (its one legitimate exit) ...
    assert http.calls[0]["json"]["document"] == _DOC
    # ... but never placed in the result output or the audit log (P7/doc 16).
    assert _DOC not in repr(res.output)
    assert _DOC not in repr(sink.read_all())
    # The grant self-destructed: it cannot be replayed.
    assert grants.consume("grant-abc") is False


def test_grant_is_single_use():
    n = ComplianceKycSubmitter()
    http = _Http(200)
    ctx, grants, sink = _harness(grant="grant-abc", http=http)
    payload = {"document_key": "passport",
               "endpoint": "https://kyc.example/submit",
               "grant_token": "grant-abc"}
    first = _run(n, payload, ctx)
    assert first.ok is True
    # Same token, second attempt — the grant is spent → fail closed.
    second = _run(n, payload, ctx)
    assert second.ok is False
    assert second.error_code == "grant_invalid"


def test_forged_or_spent_grant_fails_closed():
    n = ComplianceKycSubmitter()
    ctx, grants, sink = _harness(grant=None, http=_Http())  # nothing granted
    res = _run(n, {"document_key": "passport",
                   "endpoint": "https://kyc.example/submit",
                   "grant_token": "not-a-real-token"}, ctx)
    assert res.ok is False
    assert res.error_code == "grant_invalid"


def test_missing_document_reports_missing():
    n = ComplianceKycSubmitter()
    ctx, grants, sink = _harness(grant="g", write_doc=False, http=_Http())
    res = _run(n, {"document_key": "passport",
                   "endpoint": "https://kyc.example/submit",
                   "grant_token": "g"}, ctx)
    assert res.ok is False
    assert res.error_code == "document_missing"


def test_no_endpoint_after_valid_read_fails_clean():
    # Grant + doc are valid, but no HTTP/endpoint to submit to. The read still
    # consumed the grant (it was authorised); the submission can't proceed.
    n = ComplianceKycSubmitter()
    ctx, grants, sink = _harness(grant="g", http=None)
    res = _run(n, {"document_key": "passport", "endpoint": None,
                   "grant_token": "g"}, ctx)
    assert res.ok is False
    assert res.error_code == "no_endpoint"
    assert grants.consume("g") is False  # grant was spent on the read attempt


def test_missing_document_key_is_rejected_before_any_read():
    n = ComplianceKycSubmitter()
    ctx, grants, sink = _harness(grant="g", http=_Http())
    res = _run(n, {"grant_token": "g"}, ctx)
    assert res.ok is False
    assert res.error_code == "submission_failed"
    # Nothing was read, so the grant is untouched.
    assert grants.consume("g") is True
