/* eslint-disable no-undef */
/**
 * Service worker for the Darwin PWA escalation companion (doc 22).
 *
 * Two responsibilities:
 *   - On `push`: decode the JSON payload the backend `WebPushNotificationSink`
 *     posted, then call `registration.showNotification(...)` so the OS
 *     surfaces a banner / lock-screen alert.
 *   - On `notificationclick`: focus an existing tab if the app is open,
 *     else open a fresh one, at the deep-linked escalation URL.
 *
 * Intentionally NOT a caching SW. Push is what matters for v1 — full
 * offline support can come later. Skipping the install-time precache step
 * keeps the SW from "owning" routes it can't serve correctly.
 */
self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    // Server should always send JSON; if it doesn't, fall back to plain text.
    try {
      const text = event.data ? event.data.text() : "";
      data = { title: "Darwin", body: text || "New escalation" };
    } catch {
      data = { title: "Darwin", body: "New escalation" };
    }
  }
  const title = data.title || "Darwin escalation";
  const options = {
    body: data.body || "Owner action required",
    tag: data.tag || data.ticket_id || undefined,
    icon: "/icons/icon-192.svg",
    badge: "/icons/icon-192.svg",
    // `renotify` makes a second push with the same tag re-buzz instead of
    // silently updating in place — important for repeated escalations.
    renotify: Boolean(data.tag),
    requireInteraction: false,
    data: {
      url: data.url || (data.darwin_id ? `/darwins/${data.darwin_id}` : "/"),
      ticket_id: data.ticket_id,
      darwin_id: data.darwin_id,
    },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target =
    (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((windows) => {
        // Reuse an open Darwin tab if one is already there.
        for (const win of windows) {
          try {
            const url = new URL(win.url);
            if (url.origin === self.location.origin) {
              win.focus();
              return win.navigate(target);
            }
          } catch {
            /* not navigable — try next */
          }
        }
        return self.clients.openWindow(target);
      }),
  );
});

// Some push services (notably Mozilla autopush) send a "subscriptionchange"
// event when they rotate the underlying subscription. The PWA needs to
// re-publish to the backend so future pushes hit the new endpoint.
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((wins) => {
        for (const w of wins) {
          w.postMessage({ type: "push-subscription-change" });
        }
      }),
  );
});
