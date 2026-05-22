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

| Class | Deadline UX | Primary action |
|-------|-------------|----------------|
| `captcha` | Countdown minutes | Open Browserbase takeover |
| `kyc` | Up to 72h | Upload / link Persona flow |
| `approval` | 24h | Approve / Deny + note |
| `info` | 7d | Free-text response |
| `data` | varies | Provide structured fields |

### Inbox list (`/escalations`)

Sort: soonest deadline first. Row shows Darwin name, class, summary, time remaining.

### Detail drawer

- Evidence links (`evidence_uris`) as safe external links or presigned fetches.
- Proposed action (read-only structured).
- Response form → `POST /darwins/{id}/escalations/{eid}/respond`
- On success: toast + remove from open list + Timeline will show resolution event via WS.

### Captcha / browser takeover

Flow:

1. User opens escalation; UI fetches `GET /darwins/{id}/browser/sessions` for active session id.
2. Embed Browserbase live view per [`../engineering/15-browser-stack.md`](../engineering/15-browser-stack.md) (dashboard iframe or pop-out).
3. User completes captcha; clicks "Done — resume Darwin".
4. POST respond with `{ "action": "approved", "note": "captcha solved" }`.

Do not integrate third-party captcha-solving services.

## Mobile app (Expo, Phase 2)

Scope from [`../engineering/22-phase-two-multi-tenant-build-plan.md`](../engineering/22-phase-two-multi-tenant-build-plan.md):

```
mobile/
  app/(tabs)/
    index.tsx          # Darwin list (minimal)
    escalations.tsx    # Inbox
  src/
    notifications.ts   # FCM token register → platform
    api.ts             # Same bearer as web
```

Features:

- Push on `escalation.open` (FCM via `FCMNotificationSink`).
- Notification actions: Approve / Deny (approval class only).
- Deep link: `darwin://escalations/{darwin_id}/{escalation_id}` → full respond screen.
- Quick glance: balance, status, open escalation count.

Not in mobile MVP: Anatomy, Timeline, vault editing, spawn wizard.

### Push payload shape (contract with backend)

```json
{
  "darwin_id": "...",
  "escalation_id": "...",
  "class": "approval",
  "summary": "Spend $50 on ads",
  "deadline_at": "..."
}
```

## Notifications preferences

`/settings` (account):

- Email on escalation (all / urgent only / digest for `info`)
- Push enable/disable per device
- SMS (Phase 3 defer)

Platform `NotificationSink` handles delivery; frontend only registers tokens and preferences API when added.

## Accessibility for escalations

Approval modals trap focus; destructive deny still requires confirm for spend > threshold (configurable).
