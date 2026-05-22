# Phased delivery

The build plan for `web/` (and later `mobile/`). Aligned with [`../overview/06-roadmap-and-phases.md`](../overview/06-roadmap-and-phases.md) and engineering build plans [`20`](../engineering/20-phase-zero-runtime-build-plan.md) / [`21`](../engineering/21-phase-one-evolution-build-plan.md) / [`22`](../engineering/22-phase-two-multi-tenant-build-plan.md).

## How to read this plan

- **Phases** are the user-visible milestones (0 → 1 → 2). Each has a single goal, an exit gate, and a definition of done.
- **Epics** group related work inside a phase (`P0-A`, `P0-B`, …).
- **Tasks** are the unit of work — each is a checklist item small enough for one PR.
- **Backend deps** are listed per epic and consolidated in [§ Backend API work register](#backend-api-work-register). Frontend tasks that depend on missing endpoints are explicitly marked **⛔ blocked** so they are scheduled after the backend task lands.
- Nothing here is started yet — see [`00-README.md`](00-README.md) status. Every checkbox is `[ ]` on purpose; this doc doubles as the working tracker.

**Phase boundaries are gates, not deadlines.** A phase ships when its exit gate passes, not when the calendar week arrives. Week ranges are planning estimates only.

## Ground truth the plan is built on

The platform REST API (`src/darwin_platform/api.py`) is largely implemented, but three assumptions in the earlier drafts of these docs were wrong. The plan accounts for the real API:

| Assumption in older drafts | Reality today | Plan consequence |
|----------------------------|---------------|------------------|
| Events support cursor pagination + `neuron_id` / `category` / `tier` filters | `GET /darwins/{id}/events` is **offset-only**, no filters | Phase 0/1 filter client-side over a loaded window; server-side filters are a Phase 2 backend task |
| Events support `?since={id}` for incremental polling | No `since` param | Phase 0 polls page 0 and dedupes by `event_id`; `since` is a Phase 1 backend task |
| Ledger CSV export is a future endpoint | `GET /darwins/{id}/ledger.csv` **already exists** | Billing/Timeline can wire export from Phase 1 |
| An aggregate `GET /escalations` exists | Only per-Darwin `GET /darwins/{id}/escalations` | Inbox does N+1 across owned Darwins in Phase 2; aggregate is an optional optimization |
| A WebSocket / Anatomy / data-browser endpoint exists | None exist | All three are Phase 2 backend tasks; Phase 0/1 are poll-only |

---

## Phase 0 — Operator observability

**Weeks 0–8 (est.) · Goal:** prove a human can spawn, watch, and kill a Darwin without `curl`.

**Exit gate:** an engineer who did not write the supervisor can, using only the UI, watch a live soak-test Darwin's event stream, filter it, and kill it — with CI green on `web/`.

Backend dependency: **existing API only.**

### Epic P0-A — Project foundation

| | Task | Acceptance |
|---|------|------------|
| [ ] | Scaffold `web/` — Next.js 15 App Router, TypeScript `strict`, Tailwind, shadcn/ui init | `pnpm dev` serves an empty authenticated shell |
| [ ] | Tooling — ESLint, Prettier, `tsconfig` strict, `next.config.ts` with CSP header stub, `.env.example` | `pnpm lint` + `pnpm typecheck` pass clean |
| [ ] | CI workflow `.github/workflows/web.yml` — install / lint / typecheck / build, triggered on `web/**` | Green check on a no-op PR |
| [ ] | Design tokens — Tailwind theme for status semantics (alive/paused/quarantined/dead/killed), density spacing, mono font for ids | Token reference page renders all states |
| [ ] | Typed API client `lib/api.ts` — base URL from env, bearer header, `ApiError{status,code,detail}`, `Idempotency-Key` on money/lifecycle `POST`s | Unit test: 4xx → typed `ApiError`; idempotency header present on `kill` |
| [ ] | Dev auth `lib/auth.ts` — token-paste screen, persist `darwin_dev_token` in `localStorage`, 401 → clear + re-prompt | Pasting a seed token reaches the fleet page; bad token shows re-prompt |

### Epic P0-B — Fleet home

| | Task | Acceptance |
|---|------|------------|
| [ ] | `(dashboard)` route group + sidebar nav shell | Nav renders; active route highlighted |
| [ ] | `/` fleet page — `GET /darwins`, table of id, status, treasury, 24h net, generation, heartbeat age | All owned Darwins listed with live-ish data |
| [ ] | Status badge component — green pulse / amber / orange lock / muted / red strikethrough | Storybook (or token page) shows all five |
| [ ] | Empty state — CTA to spawn first Darwin, link to economic-risk doc | Shown when `GET /darwins` is empty |
| [ ] | TanStack Query setup — `['darwins']` key, 30s stale, manual refresh button | Refetch on focus + manual works |

> Open-escalation count per row needs an N+1 call (`GET /darwins/{id}/escalations`); in Phase 0 show it only if the fleet is small (<10), otherwise defer the column to Phase 2.

### Epic P0-C — Per-Darwin observation

| | Task | Acceptance |
|---|------|------------|
| [ ] | `/darwins/[id]` minimal shell — status header + treasury strip from `GET /darwins/{id}` | Header reflects lifecycle + balance |
| [ ] | Event table — `GET /darwins/{id}/events?limit=&offset=`, newest-first, "load older" via offset | 100-row page loads; paging older works |
| [ ] | Event row templates — `orchestrator`, `neuron_dispatch`, `llm_call`, `payment`, `escalation`, `mutation`, `owner_action`, `platform`, `death`; generic JSON expander for unknown | Each category renders its summary + expand |
| [ ] | Client-side category filter over the loaded window | Filtering by category narrows visible rows |
| [ ] | Polling — refetch page 0 every 3s when `document.visibilityState==='visible'`, dedupe by `event_id`, "N new events" chip | New soak-test events appear within ~3s |

### Epic P0-D — Kill

| | Task | Acceptance |
|---|------|------------|
| [ ] | Kill modal — confirm by typing the Darwin id (or "I understand" checkbox), `POST /darwins/{id}/kill` with `Idempotency-Key`, double-submit guard | Cannot submit without confirmation |
| [ ] | Post-kill — refresh status, toast, update fleet row to `killed` | UI reflects terminal state without manual reload |
| [ ] | Error handling — `409` already-killed → refresh + message; **never auto-retry kill** | 409 path verified against a killed Darwin |

**Explicitly out of scope for Phase 0:** Anatomy canvas, Clerk, vault, billing UI, lineage, spawn wizard, WebSocket, mobile.

**Phase 0 definition of done:** exit gate passes; `pnpm lint/typecheck/test/build` green in CI; dev-token flow documented in `web/README.md`.

---

## Phase 1 — Evolution visibility

**Weeks 8–16 (est.) · Goal:** support evolution-loop operators — lineage, mutations, treasury, and a minimal spawn path. Still operator-grade, not consumer polish.

**Exit gate:** an operator can spawn a Darwin from the UI, follow a reproduction across generations on the Lineage view, and read the treasury/cost story without leaving the dashboard.

### Epic P1-A — Spawn wizard

| | Task | Acceptance |
|---|------|------------|
| [ ] | `/darwins/new` wizard — opening budget (USDC), risk acknowledgment, read-only strategy archetype, confirm | `POST /darwins` → redirect to `/darwins/[id]` |
| [ ] | Budget validation against platform min/max (**⛔ blocked** on backend budget-bounds config — see register #6) | Out-of-range budget rejected inline |

### Epic P1-B — Lineage

| | Task | Acceptance |
|---|------|------------|
| [ ] | `/darwins/[id]/lineage` — `GET /lineage/tree`; Phase 1 minimum is an indented list | Parent→child structure visible |
| [ ] | Node click → summary panel (fitness at birth/death, cause of death, link to Timeline) | Panel populated from lineage payload |
| [ ] | d3 tree polish (optional within Phase 1; may slip to Phase 2) | Pan/zoom tree replaces the list |

### Epic P1-C — Evolution events

| | Task | Acceptance |
|---|------|------------|
| [ ] | Distinct `mutation` and `reproduction` Timeline row templates | Patch proposed/admitted/rejected and child-spawned rows are visually distinct |
| [ ] | Mutation row → deep link to proposal detail via `GET /darwins/{id}/proposals` | Clicking a mutation opens its sandbox id + fitness delta |

### Epic P1-D — Treasury & billing charts

| | Task | Acceptance |
|---|------|------------|
| [ ] | Recharts treasury chart from `GET /darwins/{id}/ledger`; burn-rate derivation | Balance-over-time + burn rate render |
| [ ] | Cost breakdown from `GET /darwins/{id}/billing` (LLM / sandbox / browser) | Numbers reconcile with ledger within rounding |
| [ ] | Ledger CSV download — wire `GET /darwins/{id}/ledger.csv` (endpoint already exists) | Download produces the accountant CSV |

### Epic P1-E — Settings stub

| | Task | Acceptance |
|---|------|------------|
| [ ] | `/darwins/[id]/settings` — pause / resume / force-reproduce, hydrated from `GET /darwins/{id}/controls` | Switches reflect server state; actions toast + refresh |

### Epic P1-F — Typed-client codegen

| | Task | Acceptance |
|---|------|------------|
| [ ] | Verify `/openapi.json` is accurate and stable (**backend register #9**) | Spec matches implemented routes |
| [ ] | Makefile target: `openapi-typescript` → `web/lib/api.generated.ts`; replace hand-written DTOs | `pnpm typecheck` passes against generated types |

**Out of scope for Phase 1:** full Anatomy, Clerk, escalation inbox, WebSocket, mobile, vault, data browser.

**Phase 1 definition of done:** exit gate passes; generated API types in use; CI green.

---

## Phase 2 — Multi-tenant alpha

**Weeks 16–28 (est.) · Goal:** friendly users fund Darwins, watch them live, and resolve escalations on their phone. This is the first user-facing quality bar.

**Exit gate:** 5+ alpha users run ≥2 owned Darwins for ≥2 weeks; ≥1 escalation resolved end-to-end via mobile push; live event latency <2s perceived.

Phase 2 has the most backend coupling — start the backend long-poles (WS, Anatomy endpoint, auth exchange) **during Phase 1** so they are ready.

### Epic P2-A — Production auth

| | Task | Backend dep |
|---|------|-------------|
| [ ] | Decide Clerk vs WorkOS at Phase 2 kickoff (integration shape is identical) | — |
| [ ] | Sign-in flow → Next.js session | — |
| [ ] | Token exchange → platform JWT scoped `owner` + `owned_darwins`, short TTL + refresh | **⛔ register #7** |
| [ ] | Move token into httpOnly cookie via a Next.js BFF route handler | — |

### Epic P2-B — Anatomy canvas

| | Task | Backend dep |
|---|------|-------------|
| [ ] | `GET /darwins/{id}/anatomy` consumed; React Flow radial layout (orchestrator center, neurons ringed by category) | **⛔ register #2** |
| [ ] | Node states — idle / active / disabled / error / dying; dispatch edges with activity-scaled thickness | register #2 |
| [ ] | Animations — grow-in on mutation, fade-collapse on removal, debounced `fitView`; respect reduced-motion | — |
| [ ] | Performance — cap 64 nodes (cluster beyond), throttle updates to 10 Hz, `memo` nodes | — |
| [ ] | Accessibility — focusable nodes, Enter selects, live-region status announcements | — |

### Epic P2-C — Timeline (full)

| | Task | Backend dep |
|---|------|-------------|
| [ ] | Full-width `/darwins/[id]/timeline` route, virtualized with `@tanstack/react-virtual` | — |
| [ ] | Server-side filters — `neuron_id` / `category` / `tier` + cursor pagination; URL-encoded shareable filters | **⛔ register #1** |
| [ ] | Cost/treasury strip reconciled against `GET /billing`; mismatch → warning badge | — |
| [ ] | Export visible events as JSONL | — |

### Epic P2-D — WebSocket realtime

| | Task | Backend dep |
|---|------|-------------|
| [ ] | `lib/ws.ts` — connect after REST snapshot, exponential backoff 1s→30s, multiplexed `timeline`/`anatomy`/`status` channels | **⛔ register #3** |
| [ ] | Reconnect resync — replay missed events via REST, re-fetch anatomy snapshot | register #1, #3 |
| [ ] | Pause-stream — buffer patches, show "buffered N", apply on resume | — |
| [ ] | `DarwinViewStore` (Zustand) — `selectedNeuronId`, `timelineFilters`, `pauseStream` | — |

### Epic P2-E — Linked canvases

| | Task |
|---|------|
| [ ] | Click neuron on Anatomy → Timeline filters to `neuron_id` |
| [ ] | Click Timeline row → Anatomy highlights neuron + edge |
| [ ] | Shared time cursor when scrubbing Timeline (optional) |

### Epic P2-F — Escalation inbox + respond

| | Task | Backend dep |
|---|------|-------------|
| [ ] | `/escalations` inbox — N+1 across owned Darwins via `GET /darwins/{id}/escalations`, sorted by deadline | optional register #5 |
| [ ] | Detail drawer — evidence links, read-only proposed action, respond form → `POST .../escalations/{eid}/respond` | — |
| [ ] | Captcha takeover — `GET /darwins/{id}/browser/sessions` → Browserbase live-view embed, "Done — resume" → respond | — |
| [ ] | Ticket-class UX — `captcha` countdown, `kyc` Persona flow, `approval`, `info`, `data` | — |

### Epic P2-G — Vault & data browser

| | Task | Backend dep |
|---|------|-------------|
| [ ] | Vault screen — owner-provided vs Darwin-created sections, provenance chips, revoke; secrets shown as fingerprint only | — |
| [ ] | Data browser — table list + paginated read-only rows, CSV export | **⛔ register #4** |

### Epic P2-H — Billing & deposits

| | Task |
|---|------|
| [ ] | Deposit / withdraw flows via `POST /platform/fiat/deposit_quote`, `confirm_deposit`, `withdraw` |
| [ ] | Harvest history + force-harvest with amount preview |

### Epic P2-I — Owner controls (complete)

| | Task |
|---|------|
| [ ] | All [`09`](09-owner-controls-escalations-mobile.md) controls — spend lock, reproduction block, disabled neurons/categories, harvest-rules CRUD |
| [ ] | Graded-response badges from `GET /darwins/{id}/alerts` on fleet + header |
| [ ] | Every mutation toast carries a "View in audit" deep link to Timeline filtered to `owner_action` |

### Epic P2-J — Mobile (Expo)

| | Task | Backend dep |
|---|------|-------------|
| [ ] | Expo app scaffold — Darwin list + escalations inbox tabs, shared API client | — |
| [ ] | FCM registration → platform; push on `escalation.open` | **⛔ register #8** |
| [ ] | Notification actions — Approve / Deny (approval class); deep link to respond screen | register #8 |

**Mobile MVP excludes:** Anatomy, Timeline, vault editing, spawn wizard.

**Phase 2 definition of done:** exit gate passes; Playwright E2E suite green; generated types current; CSP/auth review signed off.

---

## Backend API work register

Frontend milestones are gated on these. Track them alongside the frontend epics; the **needed-by** column ties each to the epic it unblocks.

| # | Endpoint / capability | Contract sketch | Needed by | Phase |
|---|----------------------|-----------------|-----------|-------|
| 1 | Events filtering + cursor pagination | `GET /darwins/{id}/events?cursor=&neuron_id=&category=&tier=` returning `{events,next_cursor}` | P2-C, P2-D | 2 |
| 2 | Anatomy snapshot | `GET /darwins/{id}/anatomy` — schema in [`05-anatomy-canvas.md`](05-anatomy-canvas.md) (`orchestrator`, `neurons[]`, `edges[]`, `heatmap`, `dna`) | P2-B | 2 |
| 3 | Darwin WebSocket stream | `WSS /darwins/{id}/stream` — multiplexed `timeline`/`anatomy`/`status`, contract in [`07-realtime-websockets-and-caching.md`](07-realtime-websockets-and-caching.md) | P2-D | 2 |
| 4 | Data browser read API | `GET /darwins/{id}/data` (RLS-safe table list) + `GET /darwins/{id}/data/{table}` (paginated rows) | P2-G | 2 |
| 5 | Aggregate escalations (optional) | `GET /escalations` owner-scoped across owned Darwins — removes inbox N+1 | P2-F | 2 |
| 6 | Budget-bounds config | Expose platform min/max opening budget so the spawn wizard validates client-side | P1-A | 1 |
| 7 | External-IdP token exchange | Platform accepts/issues a JWT for a Clerk/WorkOS identity, scoped `owner` + `owned_darwins` | P2-A | 2 |
| 8 | FCM notification sink + push payload | `FCMNotificationSink`; payload shape in [`09-owner-controls-escalations-mobile.md`](09-owner-controls-escalations-mobile.md) | P2-J | 2 |
| 9 | OpenAPI publication | `/openapi.json` accurate + stable for `openapi-typescript` codegen | P1-F | 1 |
| 10 | Incremental events polling (optional) | `?since={event_id}` so Phase 0/1 polling fetches only deltas | P0-C, P1-C | 1 |

Items 1–4, 7, 8 are **Phase 2 blockers** — they must land before their epic's first task. Schedule the long-poles (1, 2, 3, 7) to start during Phase 1.

---

## Cross-cutting workstreams

These span all phases; treat them as standing checklists, not one-time tasks.

| Workstream | Phase 0 | Phase 1 | Phase 2 |
|-----------|---------|---------|---------|
| **Design system** | Status tokens, density, badges | Chart theming | Anatomy node styles, mobile theme |
| **Testing** | Vitest units, 1 Playwright smoke | Fixture-driven event rendering tests | Anatomy visual regression, full Playwright E2E, mobile detox/smoke |
| **Accessibility** | Keyboard nav on event table, AA contrast | Focusable lineage nodes | Focusable Anatomy nodes, focus-trapped modals, reduced-motion, live regions |
| **Observability** | `X-Darwin-Client` header on mutations | Client error reporting (Sentry or equivalent) | Realtime-latency instrumentation |
| **Security** | No committed tokens, CSP stub | Secret-redaction in raw-JSON expander | CSP allowlist for Browserbase iframe, KYC presigned-URL TTL, auth review |

---

## Risk register

| Risk | Impact | Likelihood | Mitigation |
|------|--------|-----------|------------|
| Backend WebSocket (register #3) slips past Phase 2 start | Live-latency exit gate at risk | Medium | Poll fallback already shipped in P0; start #3 in Phase 1; allow alpha on poll if WS late but keep gate |
| Events API never gains server-side filters (#1) | Timeline filtering stays window-limited | Medium | Client-side window filter is acceptable through Phase 1; gate Phase 2-C on #1 |
| Anatomy schema (#2) churns after P2-B starts | React Flow rework | Medium | Freeze the `GET /anatomy` schema before P2-B task 1; lock golden JSON fixtures |
| Auth vendor (Clerk vs WorkOS) decided late | Blocks P2-A | Low | Integration shape is identical; decide at Phase 2 kickoff, no earlier dependency |
| Browserbase iframe blocked by CSP / framing | Captcha takeover broken | Medium | Spike the embed early in Phase 2; pop-out window as fallback |
| Mobile scope creep into Anatomy/Timeline | Phase 2 slips | Medium | MVP frozen at list + inbox + push; parity deferred to Phase 4 |
| Idempotency / double-kill on flaky network | Duplicate lifecycle actions | Low | `Idempotency-Key` on all money/lifecycle POSTs; never auto-retry kill |

---

## Effort estimate & critical path

| Phase | Frontend person-weeks | Critical path |
|-------|----------------------|---------------|
| 0 | 2–3 | P0-A foundation → P0-C events → P0-D kill |
| 1 | 3–4 | P1-F codegen depends on #9; rest parallelizable |
| 2 | 8–12 (incl. mobile 2–3) | register #2/#3/#7 → P2-B/P2-D/P2-A; these are the long poles |

Parallelize aggressively: within a phase, only the marked dependencies are hard ordering constraints. The Phase 2 critical path runs through **backend** work — frontend Phase 2 cannot finish faster than the WebSocket, Anatomy, and auth-exchange endpoints land.

---

## CI gates per phase

```yaml
# .github/workflows/web.yml — tighten the gate as phases land
- pnpm install --frozen-lockfile
- pnpm lint            # Phase 0
- pnpm typecheck       # Phase 0
- pnpm test            # Phase 0 (units), grows each phase
- pnpm build           # Phase 0
- pnpm test:e2e        # Phase 2 (Playwright) — smoke from Phase 0
```

Phase exit is blocked if its CI gate is not green.

---

## Phase 3+ — deferrals

| Item | When |
|------|------|
| Marketing site in Next.js | Public beta |
| Internationalization (i18n) | Post-beta |
| Full mobile parity (Anatomy, Timeline) | Phase 4 |
| Operator admin in the owner app | Never — separate internal WebAuthn-protected app |
| PWA / offline | Not planned |
| Team / org accounts | Not planned for alpha (one user, one wallet, N Darwins) |
