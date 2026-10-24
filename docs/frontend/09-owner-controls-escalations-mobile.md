# Owner controls, escalations, and mobile

Maps to [`../engineering/09-kill-switches-and-owner-controls.md`](../engineering/09-kill-switches-and-owner-controls.md) and [`../engineering/08-platform-control-plane.md`](../engineering/08-platform-control-plane.md).

## Owner controls UI

All controls live under `/darwins/[id]/settings` with dangerous actions duplicated in a header overflow menu.

| Control | API | UI pattern |
|---------|-----|------------|
| Pause | `POST .../pause` | Toggle; show "pausing at next checkpoint" |
| Resume | `POST .../resume` | Toggle |
| Terminate | `POST .../kill` | Modal + confirm |
| Spend lock | `POST .../controls/spend_lock` | Switch; tooltip: metered LLM still runs |
| Block reproduction | `POST .../controls/reproduction_block` | Switch |
| Disabled neurons | `POST .../controls/disabled_neurons` | Multi-select chips |
| Disabled categories | `POST .../controls/disabled_categories` | Multi-select |
| Force reproduce | `POST .../reproduce/force` | Button + confirm |
| Force harvest | `POST .../harvest` | Button + amount preview |
| Harvest rules | harvest_rules CRUD | Form table |

`GET .../controls` hydrates initial switch states on page load.

**Cannot do in UI (by design):**

- Loosen spend caps above DNA hard cap.
- Raise escalation threshold above DNA default.
- Resurrect killed Darwin.

## Graded response indicators

`GET .../alerts` drives badges on fleet and Darwin header:

| Level | Badge | Color |
|-------|-------|-------|
| 1 Warning | ⚠ | Yellow |
| 2 Throttle | ⏳ | Amber |
| 3 Pause | ⏸ | Orange |
| 4 Quarantine | 🔒 | Red outline |
| 5 Kill | — | Darwin already terminal |

Quarantine copy explains wallet and egress locked; link to support.

## Escalation inbox

### Ticket classes

| Class | Deadline UX | Primary action | Backend provider |
|-------|-------------|----------------|------------------|
| `captcha` | Countdown minutes | Open live browser takeover | Browserbase **or** Browser Use Cloud (deploy-time `--browser-provider`) |
| `kyc` | Up to 72h | Hand off to a hosted KYC flow | Persona **or** Onfido (deploy-time `--kyc-provider`) |
| `approval` | 24h | Approve / Deny + note | — |
| `info` | 7d | Free-text response | — |
| `data` | varies | Provide structured fields | — |

The FE renders the same UX for both captcha providers and both KYC providers; the difference is the embed URL / handoff URL the platform returns in the ticket payload. There is also a Twilio-backed SMS-2FA path (`GET /platform/sms/verification-code`) but it is **operator scope only** — it is **not** exposed in the owner UI.

### Inbox list (`/escalations`)

Sort: soonest deadline first. Row shows Darwin name, class, summary, time remaining.

### Detail drawer

- Evidence links (`evidence_uris`) as safe external links or presigned fetches.
- Proposed action (read-only structured).
- Response form → `POST /darwins/{id}/escalations/{eid}/respond`
- On success: toast + remove from open list + Timeline will show resolution event via WS.

### Captcha / browser takeover

Flow:

1. User opens escalation; UI fetches `GET /darwins/{id}/browser/sessions` for the active session id.
2. Embed the provider's live view per [`../engineering/15-browser-stack.md`](../engineering/15-browser-stack.md) / [`../engineering/31-browser-use-evaluation.md`](../engineering/31-browser-use-evaluation.md). The platform exposes the chosen provider in the session payload; the FE picks the right iframe / pop-out renderer.
3. User completes captcha; clicks "Done — resume Darwin".
4. POST respond with `{ "action": "approved", "note": "captcha solved" }`.

CSP `frame-src` must allowlist **both** Browserbase and Browser Use Cloud domains, regardless of which is active at deploy time. Do not integrate third-party captcha-solving services.

## Mobile companion (PWA, Phase 2)

The original scope in [`../engineering/22-phase-two-multi-tenant-build-plan.md`](../engineering/22-phase-two-multi-tenant-build-plan.md) was a minimal Expo app (`mobile/`, FCM token registration, Approve / Deny notification actions, `darwin://` deep links). **As built (2026-05-31):** it shipped as an installable PWA folded into `web/` instead, so the same dashboard is the mobile client and there is no `mobile/` package.

- **Install + push:** `web/public/manifest.webmanifest` and `web/public/sw.js`; the `/notifications` page asks for permission, subscribes with the platform's VAPID key (`GET /me/push-vapid-public-key`) and registers the device (`POST /me/push-subscriptions`; `GET` / `DELETE` to list / remove).
- **Delivery:** `WebPushNotificationSink` (`src/darwin_platform/escalations/notification_webpush.py`, `--push-provider webpush`) pushes on every new escalation to each of the owner's subscriptions; dead subscriptions (404/410) are purged. FCM is not used.
- **Click-through:** the service worker focuses an open dashboard tab or opens the notification's `url` (`/darwins/{darwin_id}`); the owner responds in the normal escalation UI. There are no inline Approve / Deny actions.

Not in the push payload: Anatomy, Timeline, vault editing, spawn wizard — those stay in the full dashboard.

### Push payload shape (contract with backend)

As sent by `_build_push_payload` in `notification_webpush.py`:

```json
{
  "title": "approval: payments.facilitator",
  "body": "Darwin 1a2b3c4d needs you — deadline in 3600s",
  "tag": "<ticket_id>",
  "url": "/darwins/<darwin_id>",
  "ticket_id": "...",
  "darwin_id": "...",
  "deadline_ts": 1767225600.0,
  "neuron": "payments.facilitator",
  "ticket_class": "approval"
}
```

## Notifications preferences

`/settings` (account):

- Email on escalation (all / urgent only / digest for `info`)
- Push enable/disable per device
- SMS (Phase 3 defer)

Platform `NotificationSink` handles delivery; frontend only registers tokens and preferences API when added. *As built:* only per-device push opt-in exists (`/notifications`); email and SMS preferences are not built.

## Accessibility for escalations

Approval modals trap focus; destructive deny still requires confirm for spend > threshold (configurable).
