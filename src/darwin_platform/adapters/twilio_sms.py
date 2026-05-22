"""Twilio SMS adapter — the platform's inbound 2FA-code inbox (doc 15).

The ``accounts.manager`` neuron signs up on third-party sites that send an
SMS verification code to a platform-owned Twilio number. This client
retrieves those inbound messages and extracts the code, so a platform
operator (or an automation) can answer the neuron's "need the SMS code"
escalation — doc 15's high-trust pattern, the alternative to relaying
every code by hand.

An HTTP skeleton over Twilio's REST API (Basic auth + a JSON GET) — the
same shape as the Persona / Onfido KYC adapters. No Twilio SDK dependency;
the injected ``http_client`` defaults to ``httpx.Client``. Coverage is via
a stub transport with canned Twilio JSON; production wires a real client.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

TWILIO_API = "https://api.twilio.com/2010-04-01"

# A 2FA code is a 4–8 digit run on a word boundary. Good enough for the
# common "Your code is 123456" / "G-123456 is your ..." SMS shapes;
# refining the heuristic is a soma-side concern.
_CODE_RE = re.compile(r"\b(\d{4,8})\b")


class TwilioError(Exception):
    """Twilio API or configuration error. Callers fail closed — a code
    that cannot be retrieved is an escalation, never a guess."""


def extract_verification_code(text: str) -> str | None:
    """Pull the first 4–8 digit verification code out of an SMS body, or
    ``None`` if there isn't one."""
    if not text:
        return None
    match = _CODE_RE.search(text)
    return match.group(1) if match else None


class TwilioSmsClient:
    """Reads inbound SMS for a platform-owned Twilio number."""

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        phone_number: str | None = None,
        http_client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not account_sid:
            raise TwilioError("TwilioSmsClient requires an account_sid")
        if not auth_token:
            raise TwilioError("TwilioSmsClient requires an auth_token")
        self._sid = account_sid
        # Default 'To' number — the platform's own Twilio number that
        # third-party 2FA texts land on.
        self._phone = phone_number
        self._clock = clock
        if http_client is None:
            try:
                import httpx  # type: ignore[import]
            except ImportError as exc:  # pragma: no cover - operator hint
                raise TwilioError(
                    "TwilioSmsClient requires httpx (or an injected "
                    "client): `pip install 'darwin-dna[platform]'`"
                ) from exc
            http_client = httpx.Client(
                base_url=TWILIO_API,
                auth=(account_sid, auth_token),
                timeout=20.0,
            )
        self._http = http_client

    def inbound_messages(
        self, *, to_number: str | None = None, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Recent *inbound* SMS to ``to_number`` (or the configured
        default number), newest first."""
        number = to_number or self._phone
        if not number:
            raise TwilioError("no phone number — pass to_number")
        try:
            resp = self._http.get(
                f"/Accounts/{self._sid}/Messages.json",
                params={"To": number, "PageSize": int(limit)},
            )
        except Exception as exc:
            raise TwilioError(f"Twilio messages fetch failed: {exc}") from exc
        if not (200 <= getattr(resp, "status_code", 500) < 300):
            raise TwilioError(
                f"Twilio returned {getattr(resp, 'status_code', 'unknown')}"
            )
        body = resp.json() if hasattr(resp, "json") else {}
        out: list[dict[str, Any]] = []
        for msg in body.get("messages", []) or []:
            if str(msg.get("direction", "")).lower() != "inbound":
                continue
            out.append({
                "sid": msg.get("sid", ""),
                "from": msg.get("from", ""),
                "to": msg.get("to", ""),
                "body": msg.get("body", "") or "",
                "received_at": msg.get("date_sent", ""),
            })
        return out

    def latest_verification_code(
        self, *, to_number: str | None = None,
    ) -> dict[str, Any] | None:
        """The most recent inbound SMS that carries a verification code,
        as ``{"code", "from", "received_at", "message_sid"}`` — or
        ``None`` when the inbox has no code yet."""
        for msg in self.inbound_messages(to_number=to_number):
            code = extract_verification_code(msg["body"])
            if code:
                return {
                    "code": code,
                    "from": msg["from"],
                    "received_at": msg["received_at"],
                    "message_sid": msg["sid"],
                }
        return None
