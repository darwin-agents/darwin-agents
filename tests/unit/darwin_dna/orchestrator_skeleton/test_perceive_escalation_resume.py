"""perceive — owner-resolution handling (docs/engineering/08, 16).

When the owner approves a blocked task, the genome re-queues it for retry.
Any data the owner (or the platform) attached to the approval — notably the
one-time vault grant token the platform mints for a sensitive-vault request
(doc 16) — must be threaded into the resumed task's payload so the retrying
neuron can use it. Previously only ``decision="data"`` merged the payload;
``decision="approve"`` dropped it, so a minted grant never reached the neuron.
"""

from __future__ import annotations

from types import SimpleNamespace

from darwin_dna.machinery.audit_logger import AuditLogger
from darwin_dna.orchestrator_skeleton.nodes import perceive
from darwin_dna.orchestrator_skeleton.state import OrchestratorState
from darwin_fakes.fake_platform import MemoryAuditSink, MemoryEscalationInbox


def _ctx(inbox):
    return SimpleNamespace(
        darwin_id="d1",
        escalation=inbox,
        audit=AuditLogger("d1", MemoryAuditSink()),
        clock=lambda: 0.0,
    )


def _open_state(ticket="kyc:tick-1"):
    st = OrchestratorState()
    st.open_escalations = {
        ticket: {
            "ticket": ticket,
            "neuron": "accounts.manager",
            "task": {"payload": {"service": "acme"}},
            "signature": "accounts.manager:x",
            "blocker_kind": "kyc",
            "ticket_class": "kyc",
            "opened_ts": 0.0,
            "deadline_ts": 1e18,
            "opened_tick": 0,
            "status": "open",
        }
    }
    st.pending_escalations = [ticket]
    return st


def test_approve_with_grant_token_threads_into_resumed_task():
    inbox = MemoryEscalationInbox(auto=None)
    inbox.respond("kyc:tick-1", "approve", {"grant_token": "GRANT-123"})
    st = _open_state()
    perceive._resolve_escalations(st, _ctx(inbox))
    assert len(st.resume_queue) == 1
    resumed = st.resume_queue[0]
    assert resumed["neuron"] == "accounts.manager"
    # The grant token reached the neuron's payload, alongside the original.
    assert resumed["task"]["payload"]["grant_token"] == "GRANT-123"
    assert resumed["task"]["payload"]["service"] == "acme"
    assert st.escalations_approved == 1
    # Resolved tickets are cleared from the open set.
    assert "kyc:tick-1" not in st.open_escalations


def test_plain_approve_without_data_still_resumes():
    inbox = MemoryEscalationInbox(auto=None)
    inbox.respond("kyc:tick-1", "approve", {})
    st = _open_state()
    perceive._resolve_escalations(st, _ctx(inbox))
    assert len(st.resume_queue) == 1
    # No data → payload unchanged (no spurious keys).
    assert st.resume_queue[0]["task"]["payload"] == {"service": "acme"}


def test_deny_does_not_resume():
    inbox = MemoryEscalationInbox(auto=None)
    inbox.respond("kyc:tick-1", "deny", {"grant_token": "should-be-ignored"})
    st = _open_state()
    perceive._resolve_escalations(st, _ctx(inbox))
    assert st.resume_queue == []
    assert st.escalations_rejected == 1
