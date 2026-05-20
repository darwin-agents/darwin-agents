"""Audit chain tamper resistance — red-team (docs/engineering/02, 10, 18, 24).

The audit log is the genome's load-bearing surface for "did the kill switch
trip?", "did a refusal happen?", "did this payment actually go out?". If an
attacker can edit or insert records without the next load detecting it, every
other safety property is plausibly deniable. These tests assert the chain's
verify-on-load step catches both editing an existing row and forging a new
one with a fabricated hash.
"""

from __future__ import annotations

import pytest

from darwin_dna.machinery.audit_logger import AuditError, AuditLogger
from darwin_fakes.fake_platform import MemoryAuditSink


def test_redteam_audit_tamper_detected_on_load():
    """An attacker who edits an existing audit row breaks the hash
    chain. A fresh AuditLogger reading the tampered store must raise."""
    sink = MemoryAuditSink()
    audit = AuditLogger("d-1", sink)
    for i in range(5):
        audit.log_event(f"event_{i}", {"i": i})
    # Tamper directly with a stored record.
    sink._records[2]["payload"]["i"] = 999  # noqa: SLF001
    # A fresh logger pointing at the tampered sink must refuse.
    with pytest.raises(AuditError):
        AuditLogger("d-1", sink)


def test_redteam_audit_chain_forgery_via_inserted_record():
    """Inserting a record with a fake hash must trip verification."""
    sink = MemoryAuditSink()
    audit = AuditLogger("d-1", sink)
    audit.log_event("x", {})
    # Forge a "next" record with garbage hash + prev_hash.
    sink._records.append({  # noqa: SLF001
        "seq": 1, "prev_hash": "0" * 64, "ts": 0.0, "kind": "event:fake",
        "payload": {}, "hash": "f" * 64, "darwin_id": "d-1",
    })
    with pytest.raises(AuditError):
        AuditLogger("d-1", sink)
