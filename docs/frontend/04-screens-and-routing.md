# Screens and routing

Route map for the Next.js App Router under `web/app/`. All authenticated routes live under a shared `(dashboard)` layout with sidebar nav.

## Global navigation

```
┌────────────────────────────────────────────────────────────┐
│  Darwin ▾   Fleet   Escalations (3)   Billing   Settings   │
└────────────────────────────────────────────────────────────┘
```

Escalations badge = count of open tickets across owned Darwins (`GET /escalations` aggregate).

> **As built (2026-10):** the sidebar (`web/components/layout/sidebar.tsx`) has Fleet, Get started (`/onboarding`), Escalations, Freelancers, Operator (operator-only), Notifications and Dev tokens (dev mode only). Billing and settings are per-Darwin tabs, not global entries.

## Route table

| Route | Screen | Phase | Primary APIs |
|-------|--------|-------|--------------|
| `/` | **Fleet home** — table/cards of all Darwins | 0 | `GET /darwins` |
| `/darwins/new` | **Spawn wizard** — budget, strategy ack, ToS | 1+ | `POST /darwins` |
| `/darwins/[id]` | **Darwin dashboard** — Anatomy (top/left) + Timeline (bottom) + treasury strip + open escalations | 0 (minimal) / 2 (full) | `GET /darwins/{id}`, events, alerts |
| `/darwins/[id]/timeline` | Full-width Timeline | 2 | events + WS |
| `/darwins/[id]/anatomy` | Full-width Anatomy | 2 | anatomy + WS |
| `/darwins/[id]/vault` | **Vault** — user + Darwin entries, revoke | 2 | vault CRUD |
| `/darwins/[id]/data` | **Data browser** — tabs for lifecycle, lineage, ledger and audit chain; an *Organism* tab (table list + paginated read-only rows) appears for operators when tenant namespaces are on | 2 | owner reads + `GET /platform/darwins/{id}/organism/tables[/{table}/rows]` |
| `/darwins/[id]/lineage` | **Lineage** — list view (d3 tree not built) | 1+ | `GET /lineage/tree` |
| `/darwins/[id]/billing` | Deposits, infra costs, harvest history | 2 | billing + fiat |
| `/darwins/[id]/settings` | Caps, harvest rules, controls, notifications | 1+ | controls, harvest_rules |
| `/escalations` | **Inbox** — all open tickets, sort by deadline | 2 | `GET /escalations` |
| `/notifications` | Web Push opt-in for escalations (PWA) | 2 | `/me/push-vapid-public-key`, `/me/push-subscriptions` |
| `/freelancers` | Owner's verified fiat-payout destinations | 2 | `/me/freelancers` |
| `/onboarding` | First-run steps: verify identity (KYC) → enable notifications → spawn → fund | 2 | `/platform/kyc/status`, `/platform/kyc/start`, push + spawn APIs |
| `/operator` | Fleet operator console (operator-only) | 2 | `/platform/*` |
| `/tokens` | Dev-token paste (dev auth mode only) | 0 | — |
| `/darwins/[id]/{summary,payments,preapprovals,resources,proposals,forensics,operator}` | Per-Darwin tabs; `preapprovals`, `payments` and `operator` are operator-only, `forensics` also needs `--soma-root` | 2 | ledger/billing, preapprovals, resources, proposals, forensics |

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

- d3 tree: nodes = Darwins, edges = parent → child. *(As built: the indented-list minimum below; no d3.)*
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

> **As built (2026-10):** operator screens live in the same dashboard but are scope-gated — tabs are hidden unless `/platform/capabilities` reports `is_operator` (or `organism_browser` / `forensics_viewer`), and `components/layout/operator-only.tsx` guards direct URLs. The API enforces `platform_operator` on every `/platform/*` route regardless.
