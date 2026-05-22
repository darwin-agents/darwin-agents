# Screens and routing

Route map for the Next.js App Router under `web/app/`. All authenticated routes live under a shared `(dashboard)` layout with sidebar nav.

## Global navigation

```
┌────────────────────────────────────────────────────────────┐
│  Darwin ▾   Fleet   Escalations (3)   Billing   Settings   │
└────────────────────────────────────────────────────────────┘
```

Escalations badge = count of open tickets across owned Darwins (`GET` per Darwin or future `GET /escalations` aggregate).

## Route table

| Route | Screen | Phase | Primary APIs |
|-------|--------|-------|--------------|
| `/` | **Fleet home** — table/cards of all Darwins | 0 | `GET /darwins` |
| `/darwins/new` | **Spawn wizard** — budget, strategy ack, ToS | 1+ | `POST /darwins` |
| `/darwins/[id]` | **Darwin dashboard** — Anatomy (top/left) + Timeline (bottom) + treasury strip + open escalations | 0 (minimal) / 2 (full) | `GET /darwins/{id}`, events, alerts |
| `/darwins/[id]/timeline` | Full-width Timeline | 2 | events + WS |
| `/darwins/[id]/anatomy` | Full-width Anatomy | 2 | anatomy + WS |
| `/darwins/[id]/vault` | **Vault** — user + Darwin entries, revoke | 2 | vault CRUD |
| `/darwins/[id]/data` | **Data browser** — table list | 2 | `GET .../data/{table}` (TBD) |
| `/darwins/[id]/data/[table]` | Paginated rows, read-only | 2 | same |
| `/darwins/[id]/lineage` | **Lineage tree** — d3 tree | 1+ | `GET /lineage/tree` |
| `/darwins/[id]/billing` | Deposits, infra costs, harvest history | 2 | billing + fiat |
| `/darwins/[id]/settings` | Caps, harvest rules, controls, notifications | 1+ | controls, harvest_rules |
| `/escalations` | **Inbox** — all open tickets, sort by deadline | 2 | escalations per Darwin |
| `/settings` | Account notifications, keyboard shortcut for kill | 2 | user prefs (mostly local + Clerk) |

## Fleet home (detail)

Columns / card fields:

- `darwin_id` (short id), display name (optional, user-set localStorage until profile API exists)
- Status: `alive` | `paused` | `quarantined` | `dead`
- Treasury balance (USDC), 24h net, generation
- Open escalations count
- Last heartbeat age (stale → warning icon)
- Quick actions: Open, Pause, Kill (kill only from row menu with confirm)

Empty state: CTA to spawn first Darwin with link to docs on economic risk.

## Per-Darwin dashboard layout (Phase 2)

```
┌─────────────────────────────────────────────────────────────┐
│  Treasury $12.40  │  burn $2.1/h  │  gen 2  │  ⚠ throttle │
├──────────────────────────────┬──────────────────────────────┤
│                              │  Open escalations (sidebar)  │
│      ANATOMY CANVAS          │  - Captcha #12 [Respond]     │
│      (min height 50vh)       │  - Spend $50 [Approve]       │
│                              │                              │
├──────────────────────────────┴──────────────────────────────┤
│  TIMELINE (virtualized, ~40vh, horizontal scroll optional)  │
└─────────────────────────────────────────────────────────────┘
```

Phase 0 collapses to: status header + paginated event table + kill button.

## Spawn wizard (Phase 1+)

Steps:

1. Opening budget (USDC) — validated against platform min/max.
2. Acknowledgment: non-guaranteed outcomes, possible total loss, no resurrection.
3. Optional: strategy archetype display (read-only from Layer 0 manifest).
4. Confirm → `POST /darwins` → redirect to `/darwins/[id]`.

## Vault screen

Two sections with provenance chips:

- **Owner-provided** — KYC docs, API keys the user uploaded.
- **Darwin-created** — tagged `neuron_id`, `created_at`, `purpose`; revoke triggers future escalation.

Actions: revoke, mark sensitive (POST via vault admin API when exposed to owner scope).

## Data browser

- Lists tables the platform exposes via read-only AppRole (WordPress posts, subscribers, etc.).
- No SQL editor; no writes.
- Export CSV per table (client-side from fetched page or `?format=csv` if API adds it).

## Lineage view

- d3 tree: nodes = Darwins, edges = parent → child.
- Node click → summary panel: fitness at death/birth, cause of death, link to Timeline archive.
- Phase 1 minimum: JSON tree rendered as indented list acceptable before d3 polish.

## Billing screen

Tabs:

- **Balance & deposits** — Stripe/MoonPay flows via platform fiat endpoints.
- **Metabolism** — LLM, sandbox, browser costs from `GET /billing`.
- **Harvests** — history of profit moves to owner wallet.

## Settings (per-Darwin)

Grouped controls matching [`../engineering/09-kill-switches-and-owner-controls.md`](../engineering/09-kill-switches-and-owner-controls.md):

- Lifecycle: pause, resume, terminate (modal)
- Budget: tighten caps, spend lock
- Capabilities: disable neuron/category (multi-select from registry)
- Reproduction: block, force reproduce
- Harvest: rules CRUD, force harvest now
- Notifications: push/email toggles (Phase 2)

Each successful mutation toast includes "View in audit" deep link to Timeline filtered to `owner_action` events.

## Operator vs owner

The owner app must not expose `/platform/*` operator routes. Internal operators use separate tooling (not documented here).
