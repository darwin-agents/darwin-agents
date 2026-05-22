"""Twilio SMS 2FA-inbox adapter (docs/engineering/15)."""

from __future__ import annotations

import types

import pytest

from darwin_platform.adapters.twilio_sms import (
    TwilioError,
    TwilioSmsClient,
    extract_verification_code,
)


class _StubHTTP:
    """Mimics httpx.Client.get against Twilio's Messages.json endpoint."""

    def __init__(self, *, messages=None, status=200):
        self._messages = messages if messages is not None else []
        self.status = status
        self.calls: list = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return types.SimpleNamespace(
            status_code=self.status,
            json=lambda: {"messages": self._messages},
        )


def _inbound(sid, body, *, frm="+15550001111", to="+15559990000"):
    return {
        "sid": sid, "direction": "inbound", "from": frm, "to": to,
        "body": body, "date_sent": f"sent-{sid}",
    }


# -- code extraction -------------------------------------------------------

def test_extract_code_common_shapes():
    assert extract_verification_code("Your code is 123456") == "123456"
    assert extract_verification_code(
        "G-845221 is your Google verification code"
    ) == "845221"
    assert extract_verification_code("4824 is your PIN") == "4824"


def test_extract_code_none_when_absent():
    assert extract_verification_code("Welcome to the service!") is None
    assert extract_verification_code("") is None
    assert extract_verification_code("only 12 and 3 here") is None


# -- TwilioSmsClient -------------------------------------------------------

def test_requires_account_sid_and_auth_token():
    with pytest.raises(TwilioError, match="account_sid"):
        TwilioSmsClient(account_sid="", auth_token="t", http_client=_StubHTTP())
    with pytest.raises(TwilioError, match="auth_token"):
        TwilioSmsClient(account_sid="s", auth_token="", http_client=_StubHTTP())


def test_inbound_messages_filters_to_inbound_only():
    http = _StubHTTP(messages=[
        _inbound("M1", "code 111222"),
        {"sid": "M2", "direction": "outbound-api", "from": "+15559990000",
         "to": "+15550001111", "body": "we sent it", "date_sent": "t0"},
    ])
    c = TwilioSmsClient(
        account_sid="s", auth_token="t", phone_number="+15559990000",
        http_client=http,
    )
    msgs = c.inbound_messages()
    assert [m["sid"] for m in msgs] == ["M1"]
    # the configured number is sent to Twilio as the To filter.
    assert http.calls[0][1]["To"] == "+15559990000"


def test_latest_verification_code_extracts_from_newest_with_a_code():
    http = _StubHTTP(messages=[
        _inbound("M1", "Your login code is 778899"),
        _inbound("M0", "older message, no code"),
    ])
    c = TwilioSmsClient(
        account_sid="s", auth_token="t", phone_number="+15559990000",
        http_client=http,
    )
    out = c.latest_verification_code()
    assert out == {
        "code": "778899", "from": "+15550001111",
        "received_at": "sent-M1", "message_sid": "M1",
    }


def test_latest_verification_code_none_when_no_code_present():
    http = _StubHTTP(messages=[_inbound("M1", "no digits worth having")])
    c = TwilioSmsClient(
        account_sid="s", auth_token="t", phone_number="+15559990000",
        http_client=http,
    )
    assert c.latest_verification_code() is None


def test_inbound_messages_requires_a_number():
    c = TwilioSmsClient(account_sid="s", auth_token="t", http_client=_StubHTTP())
    with pytest.raises(TwilioError, match="number"):
        c.inbound_messages()


def test_to_number_argument_overrides_the_default():
    http = _StubHTTP(messages=[])
    c = TwilioSmsClient(
        account_sid="s", auth_token="t", phone_number="+15559990000",
        http_client=http,
    )
    c.inbound_messages(to_number="+15557778888")
    assert http.calls[0][1]["To"] == "+15557778888"


def test_twilio_http_error_surfaces_as_twilio_error():
    http = _StubHTTP(status=503)
    c = TwilioSmsClient(
        account_sid="s", auth_token="t", phone_number="+15559990000",
        http_client=http,
    )
    with pytest.raises(TwilioError, match="503"):
        c.inbound_messages()


# -- CLI wiring ------------------------------------------------------------

def test_cli_parses_twilio_flags():
    from darwin_platform.__main__ import build_parser
    args = build_parser().parse_args([
        "serve",
        "--twilio-account-sid", "ACxxx",
        "--twilio-auth-token", "tok",
        "--twilio-phone-number", "+15551234567",
    ])
    assert args.twilio_account_sid == "ACxxx"
    assert args.twilio_phone_number == "+15551234567"


def test_build_platform_services_builds_the_twilio_client():
    from darwin_platform.__main__ import _build_platform_services, build_parser
    args = build_parser().parse_args([
        "serve", "--twilio-account-sid", "ACxxx", "--twilio-auth-token", "tok",
    ])
    assert isinstance(
        _build_platform_services(args).get("sms_client"), TwilioSmsClient
    )


# -- API endpoint ----------------------------------------------------------

@pytest.mark.asyncio
async def test_sms_endpoint_returns_the_latest_code():
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from darwin_platform.api import build_app
    from darwin_platform.runtime.supervisor import Supervisor

    class _StubSms:
        def latest_verification_code(self, *, to_number=None):
            return {"code": "424242", "from": "+15550001111",
                    "received_at": "t", "message_sid": "M9"}

    sup = Supervisor(heartbeat_seconds=0.0, sms_client=_StubSms())
    transport = httpx.ASGITransport(app=build_app(sup))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t",
        ) as c:
            r = await c.get("/platform/sms/verification-code")
            assert r.status_code == 200
            assert r.json()["code"] == "424242"
    finally:
        await sup.shutdown()


@pytest.mark.asyncio
async def test_sms_endpoint_409_when_no_twilio_configured():
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from darwin_platform.api import build_app
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor(heartbeat_seconds=0.0)  # no sms_client
    transport = httpx.ASGITransport(app=build_app(sup))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t",
        ) as c:
            r = await c.get("/platform/sms/verification-code")
            assert r.status_code == 409
    finally:
        await sup.shutdown()
