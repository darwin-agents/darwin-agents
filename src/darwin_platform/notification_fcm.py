"""FCM push notification sink (docs/engineering/22 §"Mobile (escalation push)").

The escalation router (:mod:`darwin_platform.escalation`) fans new
tickets out to its :class:`NotificationSink`. Phase 0–1 use
:class:`LogNotificationSink` (a no-op recorder). Phase 2 swaps in this
FCM-backed sink so mobile clients get a push the moment an owner
ticket opens.

Implementation uses Firebase Cloud Messaging HTTP v1 (the modern
endpoint; legacy server keys are deprecated). Auth is via a Google
service-account JSON; tokens are minted on demand and cached until
expiry.

The sink registers per-owner FCM device tokens (the mobile app
publishes these on login). When a ticket fires, every device token
owned by the ticket's owner gets a push.

NEVER raises on transport failure — the escalation router would crash
trying to send and the genome would observe a broken contract. FCM
errors are logged + counted so ops can see delivery health, but they
do not propagate.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from .escalation import NotificationSink, RoutedTicket


@runtime_checkable
class FCMTransport(Protocol):
    """The minimum HTTP shape :class:`FCMNotificationSink` calls. Real
    impl is a thin wrapper around ``httpx.Client``; tests inject a stub
    that records the requests."""

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]:
        """Returns ``{"status_code": int, "body": dict, "error": str | None}``."""
        ...


@runtime_checkable
class FCMTokenProvider(Protocol):
    """Mints Google service-account OAuth bearer tokens for FCM. Real
    impl uses ``google.auth``; the stub returns a fixed token."""

    def get_access_token(self) -> str: ...


@dataclass(frozen=True)
class FCMDeliveryRecord:
    """One push delivery attempt. The sink keeps a ring buffer of
    these for ops + tests; the audit-log entry is the durable record."""

    ticket_id: str
    darwin_id: str
    device_token: str
    status_code: int
    ts: float
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "darwin_id": self.darwin_id,
            "device_token_suffix": self.device_token[-6:],  # never log full token
            "status_code": self.status_code,
            "ts": self.ts,
            "error": self.error,
        }


@dataclass
class _OwnerTokens:
    """Tokens registered for one owner subject. Reverse-indexed for
    O(1) revocation on logout."""

    by_token: dict[str, float] = field(default_factory=dict)
    last_seen: float = 0.0


class FCMNotificationSink:
    """:class:`NotificationSink` backed by FCM HTTP v1.

    The sink resolves the owner of a ticket via the optional
    ``darwin_owner_resolver`` callable (the supervisor provides one
    pointing at its auth/token store). When the resolver is absent,
    pushes go to *every* registered device — fine for single-tenant
    dev, refused for production.

    Args:
        project_id: GCP project id hosting the FCM project.
        token_provider: mints OAuth tokens. Tests pass
            :class:`StubFCMTokenProvider`.
        transport: HTTP transport. Tests pass :class:`StubFCMTransport`.
        darwin_owner_resolver: ``darwin_id → owner_subject``. Optional.
        history_cap: ring-buffer size for :attr:`history`.
    """

    FCM_URL_TEMPLATE = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

    def __init__(
        self,
        *,
        project_id: str,
        token_provider: FCMTokenProvider,
        transport: FCMTransport,
        darwin_owner_resolver: Callable[[str], str | None] | None = None,
        clock: Callable[[], float] = time.time,
        history_cap: int = 256,
        request_timeout_s: float = 8.0,
    ) -> None:
        if not project_id:
            raise ValueError("FCMNotificationSink requires project_id")
        self._url = self.FCM_URL_TEMPLATE.format(project_id=project_id)
        self._tokens = token_provider
        self._transport = transport
        self._resolver = darwin_owner_resolver
        self._clock = clock
        self._timeout = float(request_timeout_s)
        self._history_cap = max(1, int(history_cap))
        self._history: list[FCMDeliveryRecord] = []
        # Owner subject → registered device tokens.
        self._owners: dict[str, _OwnerTokens] = defaultdict(_OwnerTokens)
        # Per-status-code counters for /metrics.
        self.success_count: int = 0
        self.failure_count: int = 0

    # -- registration ------------------------------------------------------
    def register_token(self, *, owner_subject: str, device_token: str) -> None:
        if not owner_subject or not device_token:
            raise ValueError("owner_subject and device_token are required")
        ownset = self._owners[owner_subject]
        ownset.by_token[device_token] = self._clock()
        ownset.last_seen = self._clock()

    def unregister_token(self, *, owner_subject: str, device_token: str) -> None:
        ownset = self._owners.get(owner_subject)
        if ownset is None:
            return
        ownset.by_token.pop(device_token, None)
        if not ownset.by_token:
            self._owners.pop(owner_subject, None)

    def tokens_for(self, owner_subject: str) -> list[str]:
        ownset = self._owners.get(owner_subject)
        if ownset is None:
            return []
        return list(ownset.by_token)

    # -- NotificationSink Protocol -----------------------------------------
    def notify(self, ticket: RoutedTicket) -> None:
        owner = self._resolve_owner(ticket.darwin_id)
        if owner is None:
            # No resolver and unable to fan-out: count as failure but
            # do NOT raise — the router must keep working.
            self._record_failure(
                ticket=ticket, device_token="",
                status_code=0, error="no owner resolver",
            )
            return
        tokens = self.tokens_for(owner)
        if not tokens:
            self._record_failure(
                ticket=ticket, device_token="",
                status_code=0, error="owner has no device tokens",
            )
            return
        # Mint one OAuth token per call — the token_provider caches its
        # own state. Failing to mint is a transport error.
        try:
            bearer = self._tokens.get_access_token()
        except Exception as exc:
            for t in tokens:
                self._record_failure(
                    ticket=ticket, device_token=t,
                    status_code=0, error=f"token mint failed: {exc}",
                )
            return
        for token in tokens:
            payload = _build_fcm_payload(token=token, ticket=ticket)
            try:
                resp = self._transport.post(
                    self._url,
                    headers={
                        "Authorization": f"Bearer {bearer}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json_body=payload,
                    timeout_s=self._timeout,
                )
            except Exception as exc:
                self._record_failure(
                    ticket=ticket, device_token=token,
                    status_code=0, error=f"transport raised: {exc}",
                )
                continue
            status = int(resp.get("status_code", 0))
            if 200 <= status < 300:
                self._record_success(ticket=ticket, device_token=token,
                                     status_code=status)
            else:
                err = str(resp.get("error") or (resp.get("body") or {}))
                self._record_failure(
                    ticket=ticket, device_token=token,
                    status_code=status, error=err[:200],
                )

    # -- read side ---------------------------------------------------------
    def history(self, *, limit: int | None = None) -> list[FCMDeliveryRecord]:
        if limit is None or limit <= 0:
            return list(self._history)
        return list(self._history[-int(limit):])

    def stats(self) -> dict[str, int]:
        return {
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "owners_registered": len(self._owners),
            "tokens_registered": sum(
                len(o.by_token) for o in self._owners.values()
            ),
        }

    # -- internals ---------------------------------------------------------
    def _resolve_owner(self, darwin_id: str) -> str | None:
        if self._resolver is None:
            return None
        try:
            return self._resolver(darwin_id)
        except Exception:
            return None

    def _record_success(
        self, *, ticket: RoutedTicket, device_token: str, status_code: int,
    ) -> None:
        rec = FCMDeliveryRecord(
            ticket_id=ticket.ticket_id, darwin_id=ticket.darwin_id,
            device_token=device_token, status_code=status_code,
            ts=self._clock(),
        )
        self._push_history(rec)
        self.success_count += 1

    def _record_failure(
        self, *, ticket: RoutedTicket, device_token: str,
        status_code: int, error: str,
    ) -> None:
        rec = FCMDeliveryRecord(
            ticket_id=ticket.ticket_id, darwin_id=ticket.darwin_id,
            device_token=device_token, status_code=status_code,
            ts=self._clock(), error=error,
        )
        self._push_history(rec)
        self.failure_count += 1

    def _push_history(self, rec: FCMDeliveryRecord) -> None:
        self._history.append(rec)
        if len(self._history) > self._history_cap:
            del self._history[: len(self._history) - self._history_cap]


def _build_fcm_payload(*, token: str, ticket: RoutedTicket) -> dict[str, Any]:
    """The FCM v1 envelope. Title + body are concise enough for a
    notification banner; full ticket details live behind a deep link
    in the mobile app the platform serves."""
    summary = (
        f"{ticket.ticket_class.value}: {ticket.neuron}"
    )
    body = (
        f"Darwin {ticket.darwin_id[:8]} needs you — "
        f"deadline in {int(ticket.remaining_seconds(ticket.deadline_ts - 60))}s"
    )
    return {
        "message": {
            "token": token,
            "notification": {
                "title": summary,
                "body": body,
            },
            "data": {
                "ticket_id": ticket.ticket_id,
                "darwin_id": ticket.darwin_id,
                "ticket_class": ticket.ticket_class.value,
                "deadline_ts": str(ticket.deadline_ts),
                "deeplink": f"darwin://escalations/{ticket.ticket_id}",
            },
            "android": {"priority": "HIGH"},
            "apns": {
                "headers": {"apns-priority": "10"},
                "payload": {"aps": {"sound": "default"}},
            },
        }
    }


# ---------------------------------------------------------------------------
# Test/dev stubs
# ---------------------------------------------------------------------------
class StubFCMTokenProvider:
    """Returns a static bearer token. Tests use this so the sink can be
    exercised without google-auth installed."""

    def __init__(self, token: str = "stub-bearer") -> None:
        self._token = token
        self.calls = 0

    def get_access_token(self) -> str:
        self.calls += 1
        return self._token


class StubFCMTransport:
    """Records every request + replays a configured response. Default
    is HTTP 200 success."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        body: dict[str, Any] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body or {"name": "projects/test/messages/test"}
        self.raises = raises
        self.requests: list[dict[str, Any]] = []

    def post(self, url, *, headers, json_body, timeout_s):
        self.requests.append({
            "url": url, "headers": dict(headers),
            "json_body": dict(json_body),
            "timeout_s": float(timeout_s),
        })
        if self.raises is not None:
            raise self.raises
        return {
            "status_code": int(self.status_code),
            "body": dict(self.body),
            "error": None if 200 <= self.status_code < 300 else "stub_error",
        }
