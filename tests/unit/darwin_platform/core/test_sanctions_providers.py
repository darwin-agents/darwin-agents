"""Tests for real sanctions providers: ChainalysisScreener + TRMScreener."""

from __future__ import annotations

import json
import types

import pytest

from darwin_platform.sanctions import (
    ChainalysisScreener,
    SanctionsScreener,
    TRMScreener,
)


class _HTTPStub:
    def __init__(self, *, status=200, body="{}", raise_on_call=None):
        self.status = status
        self.body = body
        self.raise_on_call = raise_on_call
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if self.raise_on_call:
            raise self.raise_on_call
        return types.SimpleNamespace(status_code=self.status, text=self.body)


# ---------------------------------------------------------------------------
# ChainalysisScreener
# ---------------------------------------------------------------------------

def test_protocol_satisfied():
    http = _HTTPStub()
    assert isinstance(
        ChainalysisScreener(api_key="k", http_client=http),
        SanctionsScreener,
    )
    assert isinstance(
        TRMScreener(api_key="k", http_client=http),
        SanctionsScreener,
    )


def test_chainalysis_allows_low_risk():
    http = _HTTPStub(status=200, body=json.dumps({"risk": "low"}))
    s = ChainalysisScreener(api_key="k", http_client=http)
    r = s.check("0xabc")
    assert r.allowed is True
    assert r.source == "chainalysis"


def test_chainalysis_blocks_severe_risk():
    http = _HTTPStub(status=200, body=json.dumps({"risk": "severe"}))
    s = ChainalysisScreener(api_key="k", http_client=http)
    r = s.check("0xabc")
    assert r.allowed is False
    assert "severe" in r.reason


def test_chainalysis_blocks_sanctioned_identification():
    http = _HTTPStub(status=200, body=json.dumps({
        "risk": "low",
        "sanctions": [{"name": "OFAC SDN"}],
    }))
    s = ChainalysisScreener(api_key="k", http_client=http)
    r = s.check("0xabc")
    assert r.allowed is False
    assert "sanctions" in r.reason


def test_chainalysis_fail_closed_on_http_error():
    http = _HTTPStub(status=500, body="")
    s = ChainalysisScreener(api_key="k", http_client=http)
    r = s.check("0xabc")
    assert r.allowed is False


def test_chainalysis_fail_closed_on_network_error():
    http = _HTTPStub(raise_on_call=ConnectionError("dns"))
    s = ChainalysisScreener(api_key="k", http_client=http)
    r = s.check("0xabc")
    assert r.allowed is False
    assert r.reason == "screening_unavailable"


def test_chainalysis_fail_closed_on_bad_json():
    http = _HTTPStub(status=200, body="not json")
    s = ChainalysisScreener(api_key="k", http_client=http)
    r = s.check("0xabc")
    assert r.allowed is False


def test_chainalysis_requires_api_key():
    with pytest.raises(ValueError):
        ChainalysisScreener(api_key="", http_client=_HTTPStub())


def test_chainalysis_token_header_sent():
    http = _HTTPStub(status=200, body='{"risk":"low"}')
    s = ChainalysisScreener(api_key="my-key", http_client=http)
    s.check("0xabc")
    assert http.calls[0]["kwargs"]["headers"]["Token"] == "my-key"


# ---------------------------------------------------------------------------
# TRMScreener
# ---------------------------------------------------------------------------

def test_trm_allows_clean_address():
    body = json.dumps([{
        "address": "0xabc",
        "addressRiskIndicators": [{"categoryRiskScoreLevel": "low"}],
        "entities": [],
    }])
    http = _HTTPStub(status=200, body=body)
    s = TRMScreener(api_key="k", http_client=http)
    r = s.check("0xabc")
    assert r.allowed is True


def test_trm_blocks_severe_risk():
    body = json.dumps([{
        "address": "0xabc",
        "addressRiskIndicators": [{"categoryRiskScoreLevel": "severe"}],
        "entities": [],
    }])
    http = _HTTPStub(status=200, body=body)
    s = TRMScreener(api_key="k", http_client=http)
    r = s.check("0xabc")
    assert r.allowed is False


def test_trm_blocks_sanctions_entity():
    body = json.dumps([{
        "address": "0xabc",
        "entities": [{"entity": "Bad LLC", "category": "Sanctions"}],
    }])
    http = _HTTPStub(status=200, body=body)
    s = TRMScreener(api_key="k", http_client=http)
    r = s.check("0xabc")
    assert r.allowed is False
    assert "Bad LLC" in r.reason


def test_trm_fail_closed_on_empty_response():
    http = _HTTPStub(status=200, body="[]")
    s = TRMScreener(api_key="k", http_client=http)
    r = s.check("0xabc")
    assert r.allowed is False


def test_trm_requires_api_key():
    with pytest.raises(ValueError):
        TRMScreener(api_key="", http_client=_HTTPStub())
