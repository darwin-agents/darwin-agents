"""LiteLLM admin client + per-Darwin spend reconciliation (doc 13).

These tests inject an httpx-shaped HTTP stub so the admin client is exercised
end-to-end (request shaping, auth header, response parsing, error mapping)
without any network. The reconciler is tested against a stubbed admin client.
"""

from __future__ import annotations

import pytest

from darwin_platform.adapters.litellm_admin import (
    LiteLLMAdminClient,
    LiteLLMAdminError,
    LiteLLMSpendReconciler,
    ledger_llm_spend_usdc,
)


# -- HTTP stub -------------------------------------------------------------
class _StubResponse:
    def __init__(self, status_code=200, payload=None, text="", raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


class _StubHttp:
    """Records calls; returns a per-path-suffix configured response.

    ``responses`` maps the path suffix (e.g. ``/key/generate``) to a
    ``_StubResponse`` or an ``Exception`` to raise (transport failure)."""

    def __init__(self):
        self.posts: list[dict] = []
        self.gets: list[dict] = []
        self.responses: dict[str, object] = {}

    def _resolve(self, url):
        for suffix, resp in self.responses.items():
            if url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return _StubResponse(200, {})

    def post(self, url, *, headers, json, timeout):
        self.posts.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return self._resolve(url)

    def get(self, url, *, headers, params, timeout):
        self.gets.append(
            {"url": url, "headers": headers, "params": params, "timeout": timeout}
        )
        return self._resolve(url)


def _client(http, **kw):
    return LiteLLMAdminClient(
        base_url="http://litellm:4000",
        master_key="sk-master",
        http=http,
        **kw,
    )


# -- construction ----------------------------------------------------------
def test_requires_base_url_and_master_key():
    with pytest.raises(ValueError):
        LiteLLMAdminClient(base_url="", master_key="k", http=_StubHttp())
    with pytest.raises(ValueError):
        LiteLLMAdminClient(base_url="u", master_key="", http=_StubHttp())


def test_base_url_trailing_slash_stripped():
    http = _StubHttp()
    http.responses["/key/generate"] = _StubResponse(200, {"key": "sk-1"})
    c = LiteLLMAdminClient(
        base_url="http://litellm:4000/", master_key="m", http=http,
    )
    c.generate_key(darwin_id="d1")
    assert http.posts[0]["url"] == "http://litellm:4000/key/generate"


# -- generate_key ----------------------------------------------------------
def test_generate_key_payload_and_auth():
    http = _StubHttp()
    http.responses["/key/generate"] = _StubResponse(
        200, {"key": "sk-darwin-abc", "key_name": "darwin-d1"}
    )
    c = _client(http)
    out = c.generate_key(
        darwin_id="d1",
        max_budget=12.5,
        budget_duration="1mo",
        models=("openai/tier-fast",),
        rpm_limit=60,
        tpm_limit=100000,
        metadata={"cohort": "alpha"},
    )
    assert out["key"] == "sk-darwin-abc"
    call = http.posts[0]
    assert call["url"].endswith("/key/generate")
    assert call["headers"]["Authorization"] == "Bearer sk-master"
    body = call["json"]
    assert body["key_alias"] == "darwin-d1"
    assert body["metadata"] == {"darwin_id": "d1", "cohort": "alpha"}
    assert body["max_budget"] == 12.5
    assert body["budget_duration"] == "1mo"
    assert body["models"] == ["openai/tier-fast"]
    assert body["rpm_limit"] == 60
    assert body["tpm_limit"] == 100000


def test_generate_key_requires_darwin_id():
    c = _client(_StubHttp())
    with pytest.raises(ValueError):
        c.generate_key(darwin_id="")


def test_generate_key_omits_unset_optionals():
    http = _StubHttp()
    http.responses["/key/generate"] = _StubResponse(200, {"key": "sk-x"})
    _client(http).generate_key(darwin_id="d1")
    body = http.posts[0]["json"]
    assert set(body) == {"key_alias", "metadata"}


def test_generate_key_raises_without_key_in_response():
    http = _StubHttp()
    http.responses["/key/generate"] = _StubResponse(200, {"detail": "nope"})
    with pytest.raises(LiteLLMAdminError):
        _client(http).generate_key(darwin_id="d1")


def test_non_2xx_raises_admin_error_with_body():
    http = _StubHttp()
    http.responses["/key/generate"] = _StubResponse(
        401, payload=None, text="invalid master key", raise_json=True
    )
    with pytest.raises(LiteLLMAdminError) as ei:
        _client(http).generate_key(darwin_id="d1")
    assert "401" in str(ei.value)
    assert "invalid master key" in str(ei.value)


def test_transport_failure_wrapped():
    http = _StubHttp()
    http.responses["/key/generate"] = ConnectionError("connection refused")
    with pytest.raises(LiteLLMAdminError) as ei:
        _client(http).generate_key(darwin_id="d1")
    assert "connection refused" in str(ei.value)


# -- key_info / key_spend --------------------------------------------------
def test_key_info_unwraps_info_envelope():
    http = _StubHttp()
    http.responses["/key/info"] = _StubResponse(
        200, {"info": {"spend": 3.5, "max_budget": 10.0}}
    )
    info = _client(http).key_info("sk-1")
    assert info == {"spend": 3.5, "max_budget": 10.0}
    # passes the key as a query param
    assert http.gets[0]["params"] == {"key": "sk-1"}


def test_key_spend_reads_spend_field():
    http = _StubHttp()
    http.responses["/key/info"] = _StubResponse(200, {"info": {"spend": 2.25}})
    assert _client(http).key_spend("sk-1") == 2.25


def test_key_spend_defaults_zero_when_absent():
    http = _StubHttp()
    http.responses["/key/info"] = _StubResponse(200, {"info": {}})
    assert _client(http).key_spend("sk-1") == 0.0


# -- delete_key ------------------------------------------------------------
def test_delete_key_posts_keys_array():
    http = _StubHttp()
    http.responses["/key/delete"] = _StubResponse(200, {"deleted": 1})
    _client(http).delete_key("sk-9")
    assert http.posts[0]["url"].endswith("/key/delete")
    assert http.posts[0]["json"] == {"keys": ["sk-9"]}


def test_delete_key_blank_is_noop():
    http = _StubHttp()
    _client(http).delete_key("")
    assert http.posts == []


# -- ledger_llm_spend_usdc -------------------------------------------------
def test_ledger_llm_spend_sums_only_llm_memos():
    movements = [
        {"debit": {"memo": "llm:reasoning", "amount_usdc": 0.10}},
        {"debit": {"memo": "llm:fast", "amount_usdc": 0.02}},
        {"debit": {"memo": "x402:upwork", "amount_usdc": 5.0}},
        {"credit": {"from": "revenue", "amount_usdc": 9.0}},
        {"debit": {"memo": "llm:coding", "amount_usdc": "0.40"}},
    ]
    assert ledger_llm_spend_usdc(movements) == pytest.approx(0.52)


def test_ledger_llm_spend_handles_junk():
    assert ledger_llm_spend_usdc([]) == 0.0
    assert ledger_llm_spend_usdc(["nope", {"debit": "x"}, {}]) == 0.0
    assert ledger_llm_spend_usdc(None) == 0.0


# -- LiteLLMSpendReconciler ------------------------------------------------
def _reconciler(spend, *, markup=0.10, tolerance=0.05, raise_=False):
    http = _StubHttp()
    if raise_:
        http.responses["/key/info"] = ConnectionError("down")
    else:
        http.responses["/key/info"] = _StubResponse(200, {"info": {"spend": spend}})
    admin = _client(http)
    return LiteLLMSpendReconciler(
        admin_client=admin, markup_rate=markup, tolerance_usdc=tolerance,
    )


def test_reconcile_within_tolerance_when_ledger_matches_grossed_up_proxy():
    rec = _reconciler(1.0)  # proxy cost 1.0, markup 0.10 → expected 1.10
    report = rec.reconcile_key(
        darwin_id="d1", virtual_key="sk-1", ledger_spend_usdc=1.10,
    )
    assert report.proxy_spend_usdc == 1.0
    assert report.expected_ledger_usdc == pytest.approx(1.10)
    assert report.drift_usdc == pytest.approx(0.0)
    assert report.within_tolerance is True
    assert report.error is None


def test_reconcile_flags_drift_beyond_tolerance():
    rec = _reconciler(1.0)
    report = rec.reconcile_key(
        darwin_id="d1", virtual_key="sk-1", ledger_spend_usdc=1.50,
    )
    # ledger 1.50 - expected 1.10 = 0.40 drift, tol 0.05
    assert report.drift_usdc == pytest.approx(0.40)
    assert report.within_tolerance is False


def test_reconcile_proxy_unreachable_reports_error_not_drift():
    rec = _reconciler(0.0, raise_=True)
    report = rec.reconcile_key(
        darwin_id="d1", virtual_key="sk-1", ledger_spend_usdc=2.0,
    )
    assert report.error is not None
    assert "down" in report.error
    # an unreachable proxy is availability, not a money discrepancy
    assert report.within_tolerance is True
