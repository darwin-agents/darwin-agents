"""Death-sequence commitment settlement (docs/engineering/05 step 3).

A dying Darwin must not leave parked work dangling: every in-flight
commitment (an escalation awaiting the owner, a queued resume, a pending
capability request) is explicitly abandoned and recorded, so the owner and
the platform escalation router can see it was closed by death.
"""

from __future__ import annotations

from darwin_dna.evolution import boot
from darwin_fakes import make_platform


def test_die_abandons_open_commitments(vclock):
    platform, stack = make_platform(opening_usdc=80.0, clock=vclock)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)

    # Simulate in-flight commitments at the moment of death.
    rt.state.open_escalations["esc-1"] = {
        "ticket": "esc-1", "neuron": "accounts.manager",
        "blocker_kind": "captcha", "status": "open",
    }
    rt.state.pending_escalations.append("esc-1")
    rt.state.pending_capability_requests.append("some.capability")

    rt._die("bankruptcy: test")

    kinds = [r["kind"] for r in stack.audit_sink.read_all()]
    # Each parked commitment is recorded as abandoned, then a summary.
    assert "event:death.commitment_abandoned" in kinds
    assert "event:death.commitments_settled" in kinds
    # Nothing left dangling.
    assert rt.state.open_escalations == {}
    assert rt.state.pending_escalations == []
    assert rt.state.pending_capability_requests == []
    assert rt.state.resume_queue == []


def test_die_with_no_commitments_is_clean(vclock):
    platform, stack = make_platform(opening_usdc=80.0, clock=vclock)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)

    rt._die("bankruptcy: test")

    kinds = [r["kind"] for r in stack.audit_sink.read_all()]
    # The settlement step always runs, even with nothing to abandon...
    assert "event:death.commitments_settled" in kinds
    # ...but emits no per-ticket abandonment when there is nothing parked.
    assert "event:death.commitment_abandoned" not in kinds
    # The rest of the death sequence is intact.
    assert "event:death.begin" in kinds
    assert "event:death.end" in kinds


def test_settlement_summary_counts_every_commitment_kind(vclock):
    platform, _stack = make_platform(opening_usdc=80.0, clock=vclock)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)

    rt.state.open_escalations["a"] = {"ticket": "a", "neuron": "n"}
    rt.state.open_escalations["b"] = {"ticket": "b", "neuron": "n"}
    rt.state.pending_capability_requests.extend(["cap.x", "cap.y"])
    rt.state.resume_queue.append({"task": "t"})

    summary = rt._settle_commitments()

    assert summary["abandoned_escalations"] == 2
    assert sorted(summary["abandoned_tickets"]) == ["a", "b"]
    assert summary["dropped_capability_requests"] == 2
    assert summary["dropped_resume_queue"] == 1
