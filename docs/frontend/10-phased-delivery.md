# Build plan

A single clean path from `web/` scaffold to alpha-ready dashboard + mobile companion. Aligned with [`../overview/06-roadmap-and-phases.md`](../overview/06-roadmap-and-phases.md) and engineering build plans [`20`](../engineering/20-phase-zero-runtime-build-plan.md) / [`21`](../engineering/21-phase-one-evolution-build-plan.md) / [`22`](../engineering/22-phase-two-multi-tenant-build-plan.md).

## How to read this plan

- **Milestones** are the build sequence (M0 → M4). Each ships something the next one builds on.
- **Epics** group related work inside a milestone (`M1-A`, `M1-B`, …).
- **Tasks** are checkbox-sized — one task ≈ one PR.
- **⛔ blocked** marks a task that needs a backend endpoint not yet implemented; see [§ Backend API work register](#backend-api-work-register).
- Nothing here is started yet — see [`00-README.md`](00-README.md) status. Every checkbox is `[ ]` on purpose; this doc doubles as the working tracker.

Milestones are gates, not deadlines. Week ranges are planning estimates only.

## What changed since the last plan

Most of the heavy backend integrations the dashboard depends on are **already done** in `src/darwin_platform/` (see [`../engineering/BACKLOG.md`](../engineering/BACKLOG.md)):

| Capability | Status | Wired by |
|-----------|--------|----------|
| Clerk RS256 auth + token store | ✅ done | `security/clerk.py`, `--clerk-issuer` / `--clerk-jwks-url` |
| Fiat ramps (Stripe + MoonPay) | ✅ done | `adapters/fiat.py`, `/platform/fiat/*` routes |
| KYC providers (Persona + Onfido) | ✅ done | `security/kyc.py` |
| SMS 2FA inbox (Twilio) | ✅ done | `adapters/twilio_sms.py`, `GET /platform/sms/verification-code` |
| Browser providers (Browserbase + Browser Use Cloud) | ✅ done | `--browser-provider {browserbase,browser-use-cloud}` |
| Ledger CSV export | ✅ done | `GET /darwins/{id}/ledger.csv` |
| PagerDuty alert sink | ✅ done | `observability/sinks.py` |
| Owner controls (spend lock, repro block, disabled neurons/categories) | ✅ done | `/darwins/{id}/controls/*` |
| Harvest rules CRUD, force harvest | ✅ done | `/darwins/{id}/harvest_rules`, `/darwins/{id}/harvest` |
| Quarantine state persisted | ✅ done | persistence schema v3 |

What's **still not on the API** and gates the live canvases:

- WebSocket stream (`WSS /darwins/{id}/stream`)
- Anatomy snapshot (`GET /darwins/{id}/anatomy`)
- Events filtering + cursor pagination, and `?since=` for incremental polling
- Data browser (`GET /darwins/{id}/data`, `/data/{table}`)
- Aggregate `/escalations` across owned Darwins (optional optimization)

That short list is the entire remaining backend ask for a complete frontend.

---

## Milestone 0 — Foundations

**Weeks 0–1 (est.) · Goal:** a buildable `web/` package with the typed API client, auth shim, and CI in place.

**Exit gate:** `pnpm lint && pnpm typecheck && pnpm test && pnpm build` is green in CI on a no-op PR.

### Epic M0-A — Scaffold

| | Task | Acceptance |
|---|------|------------|
| [ ] | Initialize `web/` — Next.js 15 App Router, TypeScript `strict`, Tailwind, shadcn/ui | `pnpm dev` serves an empty shell |
| [ ] | Tooling — ESLint, Prettier, `tsconfig` strict, `next.config.ts` with CSP header stub, `.env.example` | `pnpm lint` + `pnpm typecheck` pass clean |
| [ ] | CI workflow `.github/workflows/web.yml` — install / lint / typecheck / build on `web/**` | Green on a no-op PR |
| [ ] | Design tokens — Tailwind theme for status semantics (alive / paused / quarantined / dead / killed), density spacing, monospaced ids | Token reference page renders all five states |

### Epic M0-B — API client + auth

| | Task | Acceptance |
|---|------|------------|
| [ ] | Generate types from `/openapi.json` → `web/lib/api.generated.ts`; Makefile target | `pnpm typecheck` passes against generated types |
| [ ] | `lib/api.ts` — base URL from env, bearer header, `ApiError{status,code,detail}`, `Idempotency-Key` on money/lifecycle `POST`s | Unit test: 4xx → typed `ApiError`; idempotency header present on `kill` |
| [ ] | `lib/auth.ts` — dev mode reads `darwin_dev_token` from `localStorage`; production wraps Clerk `useAuth().getToken()` (the backend already accepts Clerk RS256) | Dev token paste + Clerk sign-in both reach the fleet page |
| [ ] | TanStack Query provider; query-key conventions in `lib/queryKeys.ts` | App boots with provider; reusable keys exported |

---

## Milestone 1 — Read-only fleet

**Weeks 1–3 (est.) · Goal:** an operator can sign in, list owned Darwins, watch their event stream, trace lineage and treasury, and kill one — all read-only except `kill`.

**Exit gate:** dogfoodable observation tool — an engineer who didn't write the supervisor can spawn (CLI), watch, and kill a Darwin entirely through the UI.

### Epic M1-A — Fleet home

| | Task |
|---|------|
| [ ] | `(dashboard)` route group + sidebar nav shell |
| [ ] | `/` — `GET /darwins`, table of id / status / treasury / 24h net / generation / heartbeat age |
| [ ] | Status badge component (alive / paused / quarantined / dead / killed) |
| [ ] | Empty state with CTA to spawn first Darwin |
| [ ] | Graded-response badges from `GET /darwins/{id}/alerts` on each row |
| [ ] | Per-row open-escalation count via `GET /darwins/{id}/escalations` (N+1; aggregate is register #5 if we need it) |

### Epic M1-B — Per-Darwin shell

| | Task |
|---|------|
| [ ] | `/darwins/[id]` — status header + treasury strip from `GET /darwins/{id}` |
| [ ] | Event table — `GET /darwins/{id}/events?limit=&offset=`, newest-first, "load older" via offset |
| [ ] | Row templates per category (`orchestrator`, `neuron_dispatch`, `llm_call`, `payment`, `escalation`, `mutation`, `reproduction`, `owner_action`, `platform`, `death`); JSON fallback for unknown |
| [ ] | Client-side category filter over loaded window (server filters are register #1) |
| [ ] | Polling — refetch page 0 every 3s when tab visible, dedupe by `event_id`, "N new events" chip |

### Epic M1-C — Lineage & treasury

| | Task |
|---|------|
| [ ] | `/darwins/[id]/lineage` — `GET /lineage/tree`; minimum acceptable is an indented list |
| [ ] | Node click → summary panel (fitness at birth/death, cause of death, link to Timeline) |
| [ ] | d3 lineage tree polish (optional within M1, may slip to M3) |
| [ ] | Recharts treasury chart from `GET /darwins/{id}/ledger`; burn rate |
| [ ] | Cost breakdown from `GET /darwins/{id}/billing` (LLM / sandbox / browser) |
| [ ] | Ledger CSV download — wire `GET /darwins/{id}/ledger.csv` |

### Epic M1-D — Spawn + kill

| | Task |
|---|------|
| [ ] | `/darwins/new` wizard — opening budget, risk acknowledgment, read-only strategy archetype |
| [ ] | `POST /darwins` → redirect to `/darwins/[id]`; out-of-range budget surfaces 422 inline |
| [ ] | Kill modal — type-the-id confirmation, `POST .../kill` with `Idempotency-Key`, double-submit guard, **never auto-retry** |
| [ ] | Post-kill — refresh status, toast, fleet row updates to `killed` |
| [ ] | 409 already-killed handling — refresh, friendly message |

---

## Milestone 2 — Owner controls + escalations

**Weeks 3–5 (est.) · Goal:** the dashboard can resolve every owner-facing situation today's API supports — controls, escalations of all five classes, vault read, billing/deposits/harvest.

**Exit gate:** an alpha user fund-deposits, resolves a captcha and a KYC escalation, tightens a spend cap, and harvests profit — all from the dashboard, no CLI.

### Epic M2-A — Settings & controls

| | Task |
|---|------|
| [ ] | `/darwins/[id]/settings` — hydrate from `GET .../controls` |
| [ ] | Lifecycle — pause / resume / terminate (terminate duplicated in header overflow) |
| [ ] | Spend lock toggle — `POST .../controls/spend_lock`; tooltip explains metered LLM still runs |
| [ ] | Block-reproduction toggle — `POST .../controls/reproduction_block` |
| [ ] | Disabled neurons / categories — multi-select chips from the registry; `POST .../controls/disabled_*` |
| [ ] | Force reproduce — button + confirm → `POST .../reproduce/force` |
| [ ] | Force harvest — amount preview + `POST .../harvest` |
| [ ] | Harvest rules — CRUD against `/darwins/{id}/harvest_rules` |
| [ ] | Every successful mutation toast carries a "View in audit" deep link |

### Epic M2-B — Escalation inbox

| | Task |
|---|------|
| [ ] | `/escalations` — fan out `GET /darwins/{id}/escalations` across owned Darwins, sort by soonest deadline (aggregate endpoint is register #5) |
| [ ] | Detail drawer — evidence links, proposed action (read-only), respond form → `POST .../escalations/{eid}/respond` |
| [ ] | Per-class UX: `captcha` countdown · `kyc` (Persona / Onfido) handoff · `approval` Approve/Deny + note · `info` free-text · `data` structured fields |
| [ ] | Captcha takeover — `GET /darwins/{id}/browser/sessions`, embed Browserbase or Browser Use Cloud live view; "Done — resume" → respond |
| [ ] | SMS-2FA escalations — operator-scope only today (`GET /platform/sms/verification-code`); document the path, do **not** expose in owner UI |
| [ ] | Optimistic-update policy — no optimism on respond (legal/safety); disable form until success |

### Epic M2-C — Vault & resources (read)

| | Task |
|---|------|
| [ ] | `/darwins/[id]/vault` — `GET .../vault` with owner-provided vs Darwin-created sections, provenance chips |
| [ ] | Secrets render as fingerprint + last four — never raw values |
| [ ] | Revoke action — `DELETE .../vault` |
| [ ] | `/darwins/[id]/resources` — `GET .../resources`; delete with confirm |

### Epic M2-D — Billing, deposits, harvest history

| | Task |
|---|------|
| [ ] | Billing screen — Balance & deposits · Metabolism · Harvests tabs |
| [ ] | Deposit flow — `POST /platform/fiat/deposit_quote` → `confirm_deposit`; provider chosen at platform deploy time (`--fiat-provider`) |
| [ ] | Withdraw flow — `POST /platform/fiat/withdraw` |
| [ ] | Harvest history rendered from event log + ledger; reconciled to `GET /billing` (mismatch → warning badge) |

---

## Milestone 3 — Live canvases (Anatomy + Timeline + WebSocket)

**Weeks 5–10 (est.) · Goal:** the two centerpiece canvases ship and are linked. This is the largest milestone and is **gated on four backend endpoints** ([§ register](#backend-api-work-register) #1, #2, #3, #4).

**Exit gate:** new soak-test events appear in the Timeline within 2s perceived, the Anatomy reflects the live LangGraph state, and clicking either canvas filters the other.

### Epic M3-A — Anatomy canvas ⛔ register #2

| | Task |
|---|------|
| [ ] | Consume `GET /darwins/{id}/anatomy`; React Flow radial layout (orchestrator center, neurons ringed by category) |
| [ ] | Node states — idle / active / disabled / error / dying; dispatch edges with activity-scaled thickness |
| [ ] | Animations — grow-in on mutation, fade-collapse on removal, debounced `fitView`; respect `prefers-reduced-motion` |
| [ ] | Performance — cap 64 nodes (cluster beyond by category), throttle updates to 10 Hz, `memo` custom node components |
| [ ] | Accessibility — focusable nodes, Enter selects, live-region status announcements |
| [ ] | Visual regression on golden anatomy fixtures (JSON → screenshot) |

### Epic M3-B — Timeline (full) ⛔ register #1

| | Task |
|---|------|
| [ ] | Full-width `/darwins/[id]/timeline` route, virtualized with `@tanstack/react-virtual` |
| [ ] | Server-side filters — `neuron_id` / `category` / `tier` + cursor pagination; URL-encoded shareable filters |
| [ ] | Cost / treasury strip reconciled against `GET /billing` |
| [ ] | Export visible events as JSONL |
| [ ] | Horizontal swimlane mode (optional; vertical remains default for narrow widths) |

### Epic M3-C — WebSocket realtime ⛔ register #3

| | Task |
|---|------|
| [ ] | `lib/ws.ts` — connect after REST snapshot, exponential backoff 1s → 30s cap, multiplexed `timeline` / `anatomy` / `status` channels |
| [ ] | Reconnect resync — replay missed events via `?since=` (register #6) and re-fetch anatomy snapshot |
| [ ] | Pause-stream — buffer patches, show "buffered N", apply on resume |
| [ ] | `DarwinViewStore` (Zustand) — `selectedNeuronId`, `timelineFilters`, `pauseStream` |
| [ ] | Disconnect on `/darwins/[id]` navigate-away; one socket per open detail page |

### Epic M3-D — Linked canvases

| | Task |
|---|------|
| [ ] | Click neuron on Anatomy → Timeline filters to that `neuron_id` |
| [ ] | Click Timeline row → Anatomy highlights neuron + edge |
| [ ] | Shared time cursor when scrubbing Timeline (optional) |
| [ ] | Selection state survives WS reconnect |

### Epic M3-E — Data browser ⛔ register #4

| | Task |
|---|------|
| [ ] | `/darwins/[id]/data` — table list from `GET .../data` |
| [ ] | `/darwins/[id]/data/[table]` — paginated read-only rows; CSV export |

---

## Milestone 4 — Mobile companion (Expo)

**Weeks 10–13 (est.) · Goal:** an alpha user resolves an escalation on their phone via push notification.

**Exit gate:** ≥1 alpha user resolves a captcha or approval escalation via mobile push, end-to-end, in a real-world session.

| | Task | Backend dep |
|---|------|-------------|
| [ ] | Expo app scaffold — Darwin list + escalations inbox tabs, shared API client | — |
| [ ] | FCM token registration → platform | **⛔ register #7** |
| [ ] | Push on `escalation.open`; notification actions Approve / Deny (approval class) | register #7 |
| [ ] | Deep links — `darwin://escalations/{darwin_id}/{escalation_id}` → respond screen | — |
| [ ] | Quick-glance: balance, status, open-escalation count | — |

**Mobile MVP excludes:** Anatomy, Timeline, vault editing, spawn wizard, billing. Phase 4+ deferral.

---

## Backend API work register

The complete remaining backend ask for a complete frontend. Each item is tied to the milestone epic it unblocks.

| # | Endpoint / capability | Contract sketch | Unblocks | Milestone |
|---|----------------------|-----------------|----------|-----------|
| 1 | Events filtering + cursor pagination | `GET /darwins/{id}/events?cursor=&neuron_id=&category=&tier=` → `{events,next_cursor}` | M3-B | M3 |
| 2 | Anatomy snapshot | `GET /darwins/{id}/anatomy` — schema in [`05-anatomy-canvas.md`](05-anatomy-canvas.md) (`orchestrator`, `neurons[]`, `edges[]`, `heatmap`, `dna`) | M3-A | M3 |
| 3 | Darwin WebSocket stream | `WSS /darwins/{id}/stream` multiplexed `timeline` / `anatomy` / `status` channels — contract in [`07-realtime-websockets-and-caching.md`](07-realtime-websockets-and-caching.md) | M3-C | M3 |
| 4 | Data browser read API | `GET /darwins/{id}/data` (RLS-safe table list) + `GET /darwins/{id}/data/{table}` (paginated rows) | M3-E | M3 |
| 5 | Aggregate escalations (optional) | `GET /escalations` owner-scoped across owned Darwins — removes M2-B fan-out | M2-B | M2 |
| 6 | Incremental events polling | `?since={event_id}` so polling and WS reconnect fetch deltas only | M1-B, M3-C | M1 |
| 7 | FCM notification sink + push payload | `FCMNotificationSink`; payload shape in [`09-owner-controls-escalations-mobile.md`](09-owner-controls-escalations-mobile.md) | M4 | M4 |

Items 1 / 2 / 3 are the **M3 long poles** — start them during M1–M2 so they're ready when the frontend needs them. The rest are smaller and can land alongside their consuming milestone.

Already-shipped backend pieces that earlier drafts of this plan called out as gaps:

- ✅ Clerk RS256 token verification + token store (M0-B)
- ✅ OpenAPI publication (M0-B uses it for codegen)
- ✅ Stripe + MoonPay fiat ramps (M2-D)
- ✅ Persona + Onfido KYC (M2-B)
- ✅ Twilio SMS 2FA inbox (M2-B operator path)
- ✅ Browserbase + Browser Use Cloud live sessions (M2-B captcha)
- ✅ Ledger CSV export (M1-C)
- ✅ Owner controls, harvest rules, quarantine (M2-A)

---

## Cross-cutting workstreams

| Workstream | M0 | M1 | M2 | M3 | M4 |
|-----------|----|----|----|----|----|
| **Design system** | Status tokens, density, badges | Chart theming | Modals, form components | Anatomy node styles | Mobile theme |
| **Testing** | Vitest units, 1 Playwright smoke | Event-row render tests + fixture set | Escalation respond E2E | Anatomy visual regression, WS resync E2E | Detox/smoke for push deep-link |
| **Accessibility** | Keyboard nav on event table, AA contrast | Focusable lineage nodes | Focus-trapped modals, escape semantics | Focusable Anatomy nodes, reduced-motion, live regions | Mobile a11y audit |
| **Observability** | `X-Darwin-Client` on mutations | Client error reporting (Sentry or equiv) | Mutation-level audit trail in UI | Realtime-latency instrumentation | FCM delivery telemetry |
| **Security** | No committed tokens, CSP stub | Secret-redaction in raw-JSON expander | KYC presigned-URL TTL ≤5 min | CSP allowlist for Browserbase/BU iframe, `frame-src` lockdown | Mobile keystore review |

---

## Risk register

| Risk | Impact | Likelihood | Mitigation |
|------|--------|-----------|------------|
| Backend WS (#3) slips into M3 | Live-latency exit gate at risk | Medium | Poll fallback shipped in M1; start #3 during M1; allow alpha on poll if WS late, but keep gate |
| Events filtering (#1) lands late | Timeline filtering stays window-limited until M3 | Medium | Client-side window filter is acceptable through M2 |
| Anatomy schema (#2) churns mid-M3 | React Flow rework | Medium | Freeze `GET /anatomy` schema before M3-A starts; lock golden JSON fixtures |
| Browserbase / Browser Use iframe blocked by CSP / framing | Captcha takeover broken | Medium | Spike both providers' embed in M2; pop-out window fallback |
| Clerk JWT mapping to platform `owner_id` drifts | Auth bugs in M0-B | Low | One integration test exercising `ClerkTokenStore` end-to-end before M1 |
| Idempotency / double-kill on flaky network | Duplicate lifecycle actions | Low | `Idempotency-Key` on all money/lifecycle POSTs; never auto-retry kill |
| Mobile scope creep into Anatomy/Timeline | M4 slips | Medium | MVP frozen at list + inbox + push; parity deferred to Phase 4+ |

---

## Effort estimate

| Milestone | Frontend person-weeks | Backend deps to unblock |
|-----------|----------------------|-------------------------|
| M0 Foundations | 1 | — |
| M1 Read-only fleet | 2 | register #6 (optional) |
| M2 Owner controls + escalations | 2 | register #5 (optional) |
| M3 Live canvases | 4–6 | register #1, #2, #3, #4 |
| M4 Mobile | 2–3 | register #7 |

Total frontend: **11–14 person-weeks**. Backend register items 1–4 must be scheduled to land before M3 starts; #7 before M4.

---

## CI gates

```yaml
# .github/workflows/web.yml — tighten the gate as milestones land
- pnpm install --frozen-lockfile
- pnpm lint            # M0
- pnpm typecheck       # M0
- pnpm test            # M0 (units), grows each milestone
- pnpm build           # M0
- pnpm test:e2e        # M2 (Playwright respond flow), expanded at M3
```

A milestone is not done until its CI gate is green.

---

## Out of scope (Phase 3+ deferrals)

| Item | When |
|------|------|
| Marketing site in Next.js | Public beta |
| Internationalization (i18n) | Post-beta |
| Full mobile parity (Anatomy, Timeline) | Phase 4 |
| Operator admin in the owner app | Never — separate internal WebAuthn-protected app |
| PWA / offline | Not planned |
| Team / org accounts | Not planned for alpha (one user, one wallet, N Darwins) |
| User-authored custom neurons / strategy marketplace | Not planned |
