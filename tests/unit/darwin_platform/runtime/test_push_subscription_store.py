"""Restart survival of :class:`PersistentPushSubscriptionStore` and the
sink-hydration handshake (docs/engineering/22).

Coverage:
- register → close → reopen → list still returns the row.
- revoke is durable.
- ON CONFLICT replaces the keys but preserves the (owner, endpoint) row.
- ``load_into_sink`` rehydrates the WebPushNotificationSink from rows.
- ``all()`` returns every row across owners.
"""

from __future__ import annotations

from pathlib import Path

from darwin_platform import PersistenceStore
from darwin_platform.escalations.notification_webpush import (
    WebPushNotificationSink,
)


def _make_sink():
    class _StubTransport:
        def send(self, **kw):
            return {"status_code": 201, "body": "", "error": None}

    return WebPushNotificationSink(
        vapid_private_key="key",
        vapid_subject="mailto:ops@example.com",
        transport=_StubTransport(),
        clock=lambda: 0.0,
    )


def test_register_round_trip_persists(tmp_path: Path) -> None:
    db = tmp_path / "platform.db"
    store = PersistenceStore(db, clock=lambda: 1_000.0)
    sub_store = store.push_subscription_store()
    sub_store.register(
        owner_subject="alice",
        endpoint="https://fcm.googleapis.com/wp/A",
        p256dh="p1", auth="a1",
        user_agent="Chrome/130",
    )
    store.close()

    store2 = PersistenceStore(db, clock=lambda: 1_001.0)
    rows = store2.push_subscription_store().list(owner_subject="alice")
    assert len(rows) == 1
    assert rows[0]["endpoint"] == "https://fcm.googleapis.com/wp/A"
    assert rows[0]["p256dh"] == "p1"
    assert rows[0]["user_agent"] == "Chrome/130"


def test_revoke_is_durable(tmp_path: Path) -> None:
    db = tmp_path / "platform.db"
    store = PersistenceStore(db, clock=lambda: 1_000.0)
    sub_store = store.push_subscription_store()
    sub_store.register(
        owner_subject="alice",
        endpoint="https://fcm.googleapis.com/wp/A",
        p256dh="p", auth="a",
    )
    assert sub_store.revoke(
        owner_subject="alice",
        endpoint="https://fcm.googleapis.com/wp/A",
    ) is True
    store.close()

    store2 = PersistenceStore(db)
    assert store2.push_subscription_store().list(owner_subject="alice") == []


def test_register_updates_keys_on_conflict(tmp_path: Path) -> None:
    db = tmp_path / "platform.db"
    store = PersistenceStore(db, clock=lambda: 1_000.0)
    sub_store = store.push_subscription_store()
    sub_store.register(
        owner_subject="alice",
        endpoint="https://fcm.googleapis.com/wp/A",
        p256dh="old-p", auth="old-a",
    )
    # Browser re-subscribed with new keys for the same endpoint.
    sub_store.register(
        owner_subject="alice",
        endpoint="https://fcm.googleapis.com/wp/A",
        p256dh="new-p", auth="new-a",
        user_agent="Chrome/131",
    )
    rows = sub_store.list(owner_subject="alice")
    assert len(rows) == 1
    assert rows[0]["p256dh"] == "new-p"
    assert rows[0]["auth"] == "new-a"
    assert rows[0]["user_agent"] == "Chrome/131"


def test_load_into_sink_rehydrates_all_owners(tmp_path: Path) -> None:
    db = tmp_path / "platform.db"
    store = PersistenceStore(db, clock=lambda: 1_000.0)
    sub_store = store.push_subscription_store()
    sub_store.register(
        owner_subject="alice",
        endpoint="https://fcm.googleapis.com/wp/A",
        p256dh="p1", auth="a1",
    )
    sub_store.register(
        owner_subject="alice",
        endpoint="https://updates.push.services.mozilla.com/wpush/B",
        p256dh="p2", auth="a2",
    )
    sub_store.register(
        owner_subject="bob",
        endpoint="https://fcm.googleapis.com/wp/C",
        p256dh="p3", auth="a3",
    )

    sink = _make_sink()
    sub_store.load_into_sink(sink)
    assert len(sink.subscriptions_for("alice")) == 2
    assert len(sink.subscriptions_for("bob")) == 1


def test_all_returns_every_row(tmp_path: Path) -> None:
    db = tmp_path / "platform.db"
    store = PersistenceStore(db, clock=lambda: 1_000.0)
    sub_store = store.push_subscription_store()
    sub_store.register(
        owner_subject="alice",
        endpoint="https://fcm.googleapis.com/wp/A",
        p256dh="p1", auth="a1",
    )
    sub_store.register(
        owner_subject="bob",
        endpoint="https://fcm.googleapis.com/wp/B",
        p256dh="p2", auth="a2",
    )
    rows = sub_store.all()
    owners = {owner for owner, _ in rows}
    assert owners == {"alice", "bob"}
