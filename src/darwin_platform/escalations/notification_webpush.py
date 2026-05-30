"""Web Push notification sink (docs/engineering/22 §"Mobile (escalation push)").

The mobile escalation companion is shipped as a PWA, so the push transport
is the W3C Push API + VAPID (RFC 8030 + RFC 8292), not FCM/APNs directly.
Browser vendors (Chrome → FCM, Firefox → autopush, Safari → APNs) sit
behind their own push endpoints — :class:`WebPushNotificationSink` just
VAPID-signs and ECE-encrypts a JSON payload per registered subscription.

A subscription is the W3C `PushSubscription` object the browser hands the
service worker after :meth:`PushManager.subscribe`::

    {
      "endpoint": "https://fcm.googleapis.com/wp/...",
      "keys": {"p256dh": "<base64url>", "auth": "<base64url>"}
    }

The sink keeps subscriptions indexed by Clerk ``owner_subject`` so a
ticket fans out to every device the owner has signed in on. Stale
subscriptions (HTTP 404/410) are pruned automatically.

NEVER raises on transport failure — the router would crash and the
genome would observe a broken contract. Errors are logged + counted; the
audit-log entry is the durable record.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

from .escalation import RoutedTicket


@runtime_checkable
class WebPushTransport(Protocol):
    """The minimum surface :class:`WebPushNotificationSink` calls. The
    real impl is a thin wrapper around ``pywebpush.webpush``; tests inject
    a stub that records calls.

    Returns ``{"status_code": int, "body": str | None, "error": str | None}``.
    HTTP 404/410 means the subscription is dead — the sink purges it.
    """

    def send(
        self,
        *,
        endpoint: str,
        p256dh: str,
        auth: str,
        payload: dict[str, Any],
        vapid_private_key: str,
        vapid_claims: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class PushSubscription:
    """One browser's Push API subscription. The endpoint is a per-browser
    URL the sink POSTs the encrypted payload to; ``p256dh`` + ``auth``
    are the keys the browser generated for end-to-end ECE encryption."""

    endpoint: str
    p256dh: str
    auth: str
    registered_at: float = 0.0
    user_agent: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "endpoint_origin": _endpoint_origin(self.endpoint),
            "registered_at": self.registered_at,
            "user_agent": self.user_agent,
        }


@dataclass
class _OwnerSubscriptions:
    """Subscriptions registered for one owner subject. Reverse-indexed by
    endpoint for O(1) revoke on logout."""

    by_endpoint: dict[str, PushSubscription] = field(default_factory=dict)
    last_seen: float = 0.0


@dataclass(frozen=True)
class WebPushDeliveryRecord:
    """One push attempt. Ring-buffered for ops snapshots + tests; the
    audit-log entry is the durable record. The endpoint is logged only
    by origin so the full subscription URL doesn't leak."""

    ticket_id: str
    darwin_id: str
    endpoint_origin: str
    status_code: int
    ts: float
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "darwin_id": self.darwin_id,
            "endpoint_origin": self.endpoint_origin,
            "status_code": self.status_code,
            "ts": self.ts,
            "error": self.error,
        }


# HTTP statuses that signal "this subscription is gone, never deliver to it
# again". Per RFC 8030 §5: 404 = unknown subscription, 410 = subscription
# explicitly removed.
_STALE_STATUSES = frozenset({404, 410})


class WebPushNotificationSink:
    """:class:`NotificationSink` backed by the W3C Push API + VAPID.

    Args:
        vapid_private_key: PEM-encoded EC private key (P-256) for VAPID
            signing. Same key the dashboard's public-key endpoint exposes
            so browsers can verify pushes came from this server.
        vapid_subject: ``mailto:`` URL or ``https://`` site URL identifying
            the contact point per RFC 8292 §2 (Subject header). Push
            services reject pushes without a valid subject.
        transport: HTTP+VAPID transport. Real impl wraps ``pywebpush``.
        darwin_owner_resolver: ``darwin_id → owner_subject``. Without this
            the sink has no way to fan-out and every push fails-fast.
        subscription_purge: optional ``(owner_subject, endpoint) -> None``
            callback the sink fires when a 404/410 makes it purge a
            subscription, so a persistent store can be kept in sync.
    """

    def __init__(
        self,
        *,
        vapid_private_key: str,
        vapid_subject: str,
        transport: WebPushTransport,
        darwin_owner_resolver: Callable[[str], str | None] | None = None,
        subscription_purge: Callable[[str, str], None] | None = None,
        clock: Callable[[], float] = time.time,
        history_cap: int = 256,
        request_timeout_s: float = 8.0,
    ) -> None:
        if not vapid_private_key:
            raise ValueError("WebPushNotificationSink requires vapid_private_key")
        if not vapid_subject:
            raise ValueError("WebPushNotificationSink requires vapid_subject")
        self._vapid_key = vapid_private_key
        self._vapid_subject = vapid_subject
        self._transport = transport
        self._resolver = darwin_owner_resolver
        self._purge = subscription_purge
        self._clock = clock
        self._timeout = float(request_timeout_s)
        self._history_cap = max(1, int(history_cap))
        self._history: list[WebPushDeliveryRecord] = []
        # Owner subject → registered subscriptions.
        self._owners: dict[str, _OwnerSubscriptions] = defaultdict(
            _OwnerSubscriptions,
        )
        self.success_count: int = 0
        self.failure_count: int = 0
        self.purged_count: int = 0

    # -- registration ---------------------------------------------------------
    def register_subscription(
        self,
        *,
        owner_subject: str,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: str = "",
    ) -> PushSubscription:
        if not owner_subject:
            raise ValueError("owner_subject is required")
        if not endpoint or not p256dh or not auth:
            raise ValueError("endpoint, p256dh, and auth are required")
        sub = PushSubscription(
            endpoint=endpoint, p256dh=p256dh, auth=auth,
            registered_at=self._clock(),
            user_agent=user_agent,
        )
        ownset = self._owners[owner_subject]
        ownset.by_endpoint[endpoint] = sub
        ownset.last_seen = self._clock()
        return sub

    def unregister_subscription(
        self, *, owner_subject: str, endpoint: str,
    ) -> bool:
        ownset = self._owners.get(owner_subject)
        if ownset is None:
            return False
        removed = ownset.by_endpoint.pop(endpoint, None) is not None
        if not ownset.by_endpoint:
            self._owners.pop(owner_subject, None)
        return removed

    def set_subscription_purge(
        self,
        callback: Callable[[str, str], None] | None,
    ) -> None:
        """Install or replace the 404/410-purge callback. The supervisor
        calls this after constructing both the sink and the persistent
        store so a stale subscription gets deleted from disk too."""
        self._purge = callback

    def subscriptions_for(self, owner_subject: str) -> list[PushSubscription]:
        ownset = self._owners.get(owner_subject)
        if ownset is None:
            return []
        return list(ownset.by_endpoint.values())

    def load_subscriptions(
        self, items: Iterable[tuple[str, PushSubscription]],
    ) -> None:
        """Bulk-load subscriptions on supervisor boot from a persistent
        store. Each item is ``(owner_subject, PushSubscription)``."""
        for owner_subject, sub in items:
            self._owners[owner_subject].by_endpoint[sub.endpoint] = sub

    # -- NotificationSink Protocol -------------------------------------------
    def notify(self, ticket: RoutedTicket) -> None:
        owner = self._resolve_owner(ticket.darwin_id)
        if owner is None:
            self._record_failure(
                ticket=ticket, endpoint_origin="",
                status_code=0, error="no owner resolver",
            )
            return
        subs = self.subscriptions_for(owner)
        if not subs:
            self._record_failure(
                ticket=ticket, endpoint_origin="",
                status_code=0, error="owner has no push subscriptions",
            )
            return
        payload = _build_push_payload(ticket)
        for sub in subs:
            origin = _endpoint_origin(sub.endpoint)
            try:
                resp = self._transport.send(
                    endpoint=sub.endpoint,
                    p256dh=sub.p256dh,
                    auth=sub.auth,
                    payload=payload,
                    vapid_private_key=self._vapid_key,
                    vapid_claims={
                        "sub": self._vapid_subject,
                        # Push services require an aud matching the endpoint
                        # origin per RFC 8292; pywebpush fills this from the
                        # endpoint by default, but we set it explicitly so
                        # alt-transports must too.
                        "aud": origin or sub.endpoint,
                    },
                    timeout_s=self._timeout,
                )
            except Exception as exc:
                self._record_failure(
                    ticket=ticket, endpoint_origin=origin,
                    status_code=0, error=f"transport raised: {exc}",
                )
                continue
            status = int(resp.get("status_code", 0))
            if 200 <= status < 300:
                self._record_success(
                    ticket=ticket, endpoint_origin=origin,
                    status_code=status,
                )
            elif status in _STALE_STATUSES:
                # Purge the dead subscription so we don't keep paying the
                # round-trip on every future ticket.
                self.unregister_subscription(
                    owner_subject=owner, endpoint=sub.endpoint,
                )
                self.purged_count += 1
                if self._purge is not None:
                    try:
                        self._purge(owner, sub.endpoint)
                    except Exception:
                        pass
                self._record_failure(
                    ticket=ticket, endpoint_origin=origin,
                    status_code=status,
                    error="subscription gone — purged",
                )
            else:
                err = str(resp.get("error") or resp.get("body") or "")
                self._record_failure(
                    ticket=ticket, endpoint_origin=origin,
                    status_code=status, error=err[:200],
                )

    # -- read side -----------------------------------------------------------
    def history(
        self, *, limit: int | None = None,
    ) -> list[WebPushDeliveryRecord]:
        if limit is None or limit <= 0:
            return list(self._history)
        return list(self._history[-int(limit):])

    def stats(self) -> dict[str, int]:
        return {
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "purged_count": self.purged_count,
            "owners_registered": len(self._owners),
            "subscriptions_registered": sum(
                len(o.by_endpoint) for o in self._owners.values()
            ),
        }

    # -- internals -----------------------------------------------------------
    def _resolve_owner(self, darwin_id: str) -> str | None:
        if self._resolver is None:
            return None
        try:
            return self._resolver(darwin_id)
        except Exception:
            return None

    def _record_success(
        self, *, ticket: RoutedTicket, endpoint_origin: str, status_code: int,
    ) -> None:
        self._push_history(WebPushDeliveryRecord(
            ticket_id=ticket.ticket_id, darwin_id=ticket.darwin_id,
            endpoint_origin=endpoint_origin, status_code=status_code,
            ts=self._clock(),
        ))
        self.success_count += 1

    def _record_failure(
        self, *, ticket: RoutedTicket, endpoint_origin: str,
        status_code: int, error: str,
    ) -> None:
        self._push_history(WebPushDeliveryRecord(
            ticket_id=ticket.ticket_id, darwin_id=ticket.darwin_id,
            endpoint_origin=endpoint_origin, status_code=status_code,
            ts=self._clock(), error=error,
        ))
        self.failure_count += 1

    def _push_history(self, rec: WebPushDeliveryRecord) -> None:
        self._history.append(rec)
        if len(self._history) > self._history_cap:
            del self._history[: len(self._history) - self._history_cap]


def _build_push_payload(ticket: RoutedTicket) -> dict[str, Any]:
    """The JSON the service worker decrypts and passes to ``self.registration
    .showNotification(...)``. Kept compact — push services cap payloads
    around 4 kB. Detail-rich data lives behind the deep link."""
    title = f"{ticket.ticket_class.value}: {ticket.neuron}"
    body = f"Darwin {ticket.darwin_id[:8]} needs you"
    deadline = ticket.deadline_ts
    if deadline:
        body += f" — deadline in {int(max(0, deadline - time.time()))}s"
    return {
        "title": title,
        "body": body,
        "tag": ticket.ticket_id,
        "url": f"/darwins/{ticket.darwin_id}",
        "ticket_id": ticket.ticket_id,
        "darwin_id": ticket.darwin_id,
        "deadline_ts": ticket.deadline_ts,
        "neuron": ticket.neuron,
        "ticket_class": ticket.ticket_class.value,
    }


def _endpoint_origin(endpoint: str) -> str:
    """Cheap scheme://host extraction so we can log without leaking the
    subscription path (which is sensitive — anyone with the full endpoint
    + VAPID key can deliver to that browser)."""
    if not endpoint:
        return ""
    # Avoid urllib.parse for a hot path that needs to be cheap; the
    # endpoint always starts ``https://`` per RFC 8030 §5.
    try:
        scheme, rest = endpoint.split("://", 1)
        host = rest.split("/", 1)[0]
        return f"{scheme}://{host}"
    except Exception:
        return ""


# -- real-pywebpush transport ------------------------------------------------
class PyWebPushTransport:
    """Production transport — wraps ``pywebpush.webpush`` which handles VAPID
    signing + RFC 8188 ECE payload encryption.

    The pywebpush import is lazy so the platform extra works without
    the [webpush] extra installed for deployers using FCM directly.
    """

    def __init__(self) -> None:
        try:
            from pywebpush import webpush  # noqa: F401
        except ImportError as exc:  # pragma: no cover - operator hint
            raise ImportError(
                "WebPushNotificationSink needs pywebpush: "
                "pip install 'darwin-platform[webpush]'"
            ) from exc

    def send(
        self,
        *,
        endpoint: str,
        p256dh: str,
        auth: str,
        payload: dict[str, Any],
        vapid_private_key: str,
        vapid_claims: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]:
        import json
        from pywebpush import WebPushException, webpush
        try:
            resp = webpush(
                subscription_info={
                    "endpoint": endpoint,
                    "keys": {"p256dh": p256dh, "auth": auth},
                },
                data=json.dumps(payload),
                vapid_private_key=vapid_private_key,
                vapid_claims=dict(vapid_claims),
                timeout=timeout_s,
            )
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", 0)
            return {
                "status_code": int(status) if status else 0,
                "body": getattr(getattr(exc, "response", None), "text", None),
                "error": str(exc)[:300],
            }
        return {
            "status_code": int(getattr(resp, "status_code", 200)),
            "body": getattr(resp, "text", None),
            "error": None,
        }
