"""The platform-side escalation router (docs/engineering/08, doc 09).

When a Darwin's neuron blocks on something only the owner can resolve — a
captcha, a KYC document request, a spend approval, a question — the genome
posts a ticket to its ``EscalationInbox`` and parks the task in working
memory. The genome already classifies and enforces its own deadlines (see
``darwin_dna.constitution.escalation_rules`` + ``perceive`` node) so a Darwin
is *never* silently stuck (P4).

This module is the **platform side**:

- **Classify-and-decorate.** Each ticket the genome posts is enriched with
  the same ``TicketClass`` + ``deadline_ts`` the genome uses, so the
  dashboard and any HTTP client see exactly the same timeline the Darwin's
  perceive node sees.
- **Notify.** Push/email/SMS fan-out via a pluggable :class:`NotificationSink`.
  Default :class:`LogNotificationSink` records calls for tests and ops; real
  deployments swap in a sink that talks to Twilio / FCM / SES.
- **Surface.** The router holds an enriched view of every open ticket so the
  Supervisor's HTTP surface can render countdowns and statuses without
  having to re-derive them from raw inbox payloads.

The router **does not** impose timeouts itself. The genome's perceive node is
the authoritative timeout sweeper (it auto-denies past-deadline tickets and
counts them as rejected escalations for the fitness function). Duplicating
the enforcement here would create two writers of "the decision" and risk a
race; the platform observes and labels, the genome decides.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable

from darwin_dna.constitution import TicketClass, ticket_timeout_seconds


class TicketStatus(str, Enum):
    OPEN = "open"
    RESPONDED = "responded"
    EXPIRED = "expired"  # observational only — the genome decides the outcome


@dataclass
class RoutedTicket:
    """One escalation as the platform sees it. ``status``/``response_*``
    transitions on a real owner response; ``EXPIRED`` is observational and
    derived from ``deadline_ts`` vs. the current clock."""

    ticket_id: str
    darwin_id: str
    neuron: str
    ticket_class: TicketClass
    opened_ts: float
    deadline_ts: float
    detail: dict[str, Any] = field(default_factory=dict)
    status: TicketStatus = TicketStatus.OPEN
    response_decision: str | None = None
    response_data: dict[str, Any] | None = None
    responded_ts: float | None = None
    notification_sent_ts: float | None = None
    tick: int | None = None  # the heartbeat the genome filed at, if known

    def remaining_seconds(self, now: float) -> float:
        return max(0.0, self.deadline_ts - now)

    def age_seconds(self, now: float) -> float:
        return max(0.0, now - self.opened_ts)

    def is_past_deadline(self, now: float) -> bool:
        return now > self.deadline_ts

    def to_dict(self, now: float) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "darwin_id": self.darwin_id,
            "neuron": self.neuron,
            "ticket_class": self.ticket_class.value,
            "status": self.status.value,
            "opened_ts": self.opened_ts,
            "deadline_ts": self.deadline_ts,
            "age_seconds": self.age_seconds(now),
            "remaining_seconds": self.remaining_seconds(now),
            "past_deadline": self.is_past_deadline(now),
            "tick": self.tick,
            "detail": dict(self.detail),
            "response": (
                None
                if self.response_decision is None
                else {
                    "decision": self.response_decision,
                    "data": dict(self.response_data or {}),
                    "responded_ts": self.responded_ts,
                }
            ),
            "notification_sent_ts": self.notification_sent_ts,
        }


# -- notification fan-out --------------------------------------------------
@runtime_checkable
class NotificationSink(Protocol):
    """Where the router fans new tickets out for owner notification. Real
    impls hit Twilio / FCM / SES; the default sink records calls for tests
    and ops without doing any network I/O."""

    def notify(self, ticket: RoutedTicket) -> None: ...


class LogNotificationSink:
    """Default no-op sink. Records every notify() call so tests + the ops
    snapshot endpoint can see what *would* have been sent. Never raises."""

    def __init__(self) -> None:
        self.notifications: list[dict[str, Any]] = []

    def notify(self, ticket: RoutedTicket) -> None:
        # Snapshot the ticket at notification time — later mutation must not
        # rewrite the record of what was sent.
        self.notifications.append(
            {
                "ticket_id": ticket.ticket_id,
                "darwin_id": ticket.darwin_id,
                "ticket_class": ticket.ticket_class.value,
                "neuron": ticket.neuron,
                "opened_ts": ticket.opened_ts,
                "deadline_ts": ticket.deadline_ts,
            }
        )


# -- the router ------------------------------------------------------------
class EscalationRouter:
    """In-memory escalation ledger + notification dispatcher.

    The Supervisor constructs one router per process and feeds it every
    ticket it sees on a Darwin's inbox. Tickets are de-duplicated by
    ``ticket_id`` (per genome convention: ``<neuron>:tick-<N>`` is unique
    within one life). A ticket the router has already seen does not
    re-notify on a second ``register()`` call — idempotent re-observation
    is how the Supervisor's heartbeat sweep works (look at every inbox
    entry every tick; only the first sighting fires a notification).
    """

    def __init__(
        self,
        *,
        sink: NotificationSink | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._sink: NotificationSink = sink or LogNotificationSink()
        self._clock = clock
        self._tickets: dict[str, RoutedTicket] = {}
        self._by_darwin: dict[str, list[str]] = {}

    # -- registration / response -----------------------------------------
    def register(self, payload: dict[str, Any]) -> RoutedTicket:
        """Idempotently route a ticket the genome just posted to its inbox.

        Expects the genome's posted payload shape::

            {"darwin_id": ..., "ticket": ..., "neuron": ...,
             "ticket_class": "captcha"|"kyc"|"approval"|"info",
             "tick": <int>, "detail": {...}}

        A second call with the same ``ticket`` id returns the existing
        record without re-notifying — the Supervisor's drive loop calls
        ``register`` every heartbeat for every visible ticket, and we want
        the side-effect (notify) to fire exactly once."""
        tid = payload.get("ticket")
        if not tid:
            raise ValueError("escalation payload missing 'ticket' id")
        existing = self._tickets.get(tid)
        if existing is not None:
            return existing

        darwin_id = payload.get("darwin_id") or "unknown"
        neuron = payload.get("neuron") or "unknown"
        # The genome puts the class in the payload. If absent or bogus, fall
        # back to INFO — the most lenient deadline — so a misclassification
        # never prematurely expires a real owner ask.
        raw_class = payload.get("ticket_class")
        try:
            klass = TicketClass(raw_class) if raw_class else TicketClass.INFO
        except ValueError:
            klass = TicketClass.INFO
        # The genome doesn't include opened_ts in the posted payload; we
        # stamp it from our clock at first sighting, which is within one
        # heartbeat of the genome's own opened_ts. The class timeout is
        # imported from the genome — single source of truth.
        opened_ts = float(payload.get("opened_ts", self._clock()))
        deadline_ts = float(payload.get("deadline_ts",
                                        opened_ts + ticket_timeout_seconds(klass)))
        ticket = RoutedTicket(
            ticket_id=tid,
            darwin_id=darwin_id,
            neuron=neuron,
            ticket_class=klass,
            opened_ts=opened_ts,
            deadline_ts=deadline_ts,
            detail=dict(payload.get("detail", {})),
            tick=payload.get("tick"),
        )
        self._tickets[tid] = ticket
        self._by_darwin.setdefault(darwin_id, []).append(tid)

        # Fire notification. A misbehaving sink must not block the platform
        # or repeat-fire on subsequent register() calls (the ticket is
        # already in _tickets, so the early-return guards us above).
        try:
            self._sink.notify(ticket)
            ticket.notification_sent_ts = self._clock()
        except Exception:
            ticket.notification_sent_ts = None
        return ticket

    def mark_responded(
        self,
        ticket_id: str,
        decision: str,
        data: dict[str, Any] | None = None,
    ) -> RoutedTicket | None:
        """Record the owner's decision. Does NOT push to the genome — that
        is the Supervisor's job via the fake inbox's ``respond`` method."""
        tk = self._tickets.get(ticket_id)
        if tk is None:
            return None
        tk.status = TicketStatus.RESPONDED
        tk.response_decision = decision
        tk.response_data = dict(data or {})
        tk.responded_ts = self._clock()
        return tk

    # -- reads -----------------------------------------------------------
    def get(self, ticket_id: str) -> RoutedTicket | None:
        tk = self._tickets.get(ticket_id)
        if tk is None:
            return None
        self._refresh_status(tk)
        return tk

    def for_darwin(
        self, darwin_id: str, *, include_resolved: bool = False
    ) -> list[RoutedTicket]:
        """Tickets the router has seen for one Darwin, newest last. By
        default only OPEN/EXPIRED (still-actionable or still-visible);
        ``include_resolved=True`` also returns RESPONDED ones."""
        out: list[RoutedTicket] = []
        for tid in self._by_darwin.get(darwin_id, []):
            tk = self._tickets[tid]
            self._refresh_status(tk)
            if not include_resolved and tk.status == TicketStatus.RESPONDED:
                continue
            out.append(tk)
        return out

    def all_for_darwin(self, darwin_id: str) -> list[RoutedTicket]:
        """Every ticket the router has ever seen for one Darwin, in arrival
        order. Used by the snapshot endpoint and audit views."""
        return [self._tickets[tid] for tid in self._by_darwin.get(darwin_id, [])]

    def _refresh_status(self, tk: RoutedTicket) -> None:
        # Observational EXPIRED labelling: never overwrites RESPONDED.
        if tk.status == TicketStatus.OPEN and tk.is_past_deadline(self._clock()):
            tk.status = TicketStatus.EXPIRED

    # -- sink access for ops/tests ---------------------------------------
    @property
    def sink(self) -> NotificationSink:
        return self._sink
