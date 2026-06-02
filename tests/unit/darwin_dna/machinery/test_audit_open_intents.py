"""AuditLogger.open_intents — dangling write-before-act intents (doc 18 #4).

A crash between an on-chain transfer and its log_outcome leaves an intent with
no terminal record. open_intents() surfaces exactly those so the platform can
reconcile them on restart.
"""

from __future__ import annotations

from darwin_dna.machinery.audit_logger import AuditLogger


class _Sink:
    def __init__(self):
        self._rows = []

    def append(self, record):
        self._rows.append(record)

    def read_all(self):
        return list(self._rows)


def _logger():
    return AuditLogger("d-test", _Sink())


def test_closed_intent_is_not_open():
    log = _logger()
    iid = log.log_intent("payment.intent", {"amount_usdc": 5.0})
    log.log_outcome(iid, "ok", {"tx": "0xabc"})
    assert log.open_intents() == []


def test_dangling_intent_is_open():
    log = _logger()
    log.log_intent("payment.intent", {"amount_usdc": 5.0})  # crash before outcome
    opens = log.open_intents()
    assert len(opens) == 1
    assert opens[0]["kind"] == "intent:payment.intent"


def test_mixed_intents():
    log = _logger()
    iid1 = log.log_intent("payment.intent", {"n": 1})
    log.log_intent("payment.intent", {"n": 2})  # dangling
    log.log_outcome(iid1, "ok")
    log.log_event("heartbeat", {})  # unrelated record
    opens = log.open_intents()
    assert len(opens) == 1
    assert opens[0]["payload"]["n"] == 2
