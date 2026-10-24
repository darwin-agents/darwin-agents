# Architecture and API integration

## Request flow

```
┌─────────────┐     HTTPS (REST)      ┌──────────────────┐
│  Next.js    │ ────────────────────► │  FastAPI         │
│  RSC + CC   │     Authorization:    │  api.py          │
│             │     Bearer <token>      │  + middleware    │
└──────┬──────┘                       └────────┬─────────┘
       │                                       │
       │  WSS /darwins/{id}/stream             │ Supervisor,
       │  (M3 — shipped, routers/stream.py)    │ persistence,
       └──────────────────────────────────────►│ read-only DB
```

1. User signs in via Clerk (production) or pastes a dev token (local dev only).
2. Clerk's RS256 JWT is sent directly as `Authorization: Bearer` — the backend's `ClerkTokenStore` (`security/clerk.py`) maps claims → `scope=owner` and `owned_darwins=[...]`. No exchange step.
3. All data mutations and reads use that token against `NEXT_PUBLIC_API_BASE_URL`.
4. Realtime views open a WebSocket after the REST snapshot loads (see [`07-realtime-websockets-and-caching.md`](07-realtime-websockets-and-caching.md)) — shipped in `routers/stream.py`; the Anatomy page uses it today.

## API surface the UI depends on

Implemented today in `src/darwin_platform/api.py` (representative list; OpenAPI is source of truth once published):

| Area | Methods | Purpose |
|------|---------|---------|
| Lifecycle | `POST/GET /darwins`, `GET /darwins/{id}`, pause, resume, kill, force reproduce | Fleet and per-Darwin status |
| Events | `GET /darwins/{id}/events` (offset, `cursor`, `since`; `kind` / `neuron_id` / `tier` filters) | Timeline |
| Ledger | `GET /darwins/{id}/ledger`, `GET /darwins/{id}/ledger.csv` | Money trail + accountant export |
| Escalations | per-Darwin `GET`, `GET/{id}`, `POST .../respond` | Inbox and resolution |
| Lineage | `GET /darwins/{id}/lineage`, `/lineage/tree` | Tree visualization |
| Owner controls | `POST /controls/*`, `GET /controls` | Spend lock, blocks, disabled neurons/categories |
| Harvest | `POST /harvest`, `harvest_rules` CRUD | Profit extraction |
| Proposals | `GET /darwins/{id}/proposals` | Mutation sandbox detail (deep-link from Timeline) |
| Billing | `GET /darwins/{id}/billing` | Cost breakdown |
| Vault | `GET/POST/DELETE /darwins/{id}/vault` | Credential browser |
| Resources | `GET/DELETE /darwins/{id}/resources` | Provisioned DBs, buckets |
| Alerts | `GET /darwins/{id}/alerts` | Graded response badges |
| Browser | `GET /darwins/{id}/browser/sessions` | Live session list for Browserbase / Browser Use Cloud takeover |
| Fiat | `POST /platform/fiat/{deposit_quote,confirm_deposit,withdraw}` | Stripe + MoonPay deposits + withdraws |
| Health | `GET /healthz`, `/readyz`, `/metrics` | Connectivity + Prometheus scrape |

**Former backend work register — all shipped** (see [`10-phased-delivery.md`](10-phased-delivery.md) "Backend API work register"):

| # | Endpoint | Consumer | Where |
|---|----------|----------|-------|
| 1 | `GET /darwins/{id}/events?cursor=&kind=&neuron_id=&tier=` (filtering + head cursor) | Timeline M3-B | `routers/darwins.py` |
| 2 | `GET /darwins/{id}/anatomy` | Anatomy snapshot M3-A | `routers/darwins.py`, `Supervisor.anatomy` |
| 3 | `WS /darwins/{id}/stream` (multiplexed `timeline` / `anatomy` / `status` / `escalations`) | Live canvases M3-C | `routers/stream.py` |
| 4 | `GET /platform/darwins/{id}/organism/tables` + `/organism/tables/{table}/rows` (operator scope) | Data browser M3-E | `routers/platform.py` |
| 5 | `GET /escalations` (owner-scoped aggregate) | Inbox M2-B | `routers/escalations.py` |
| 6 | `?since={seq}` tail cursor on the events endpoint | Polling + WS resync | `routers/darwins.py` |
| 7 | Push sink + payload — shipped as W3C Web Push (`WebPushNotificationSink`), not FCM | PWA M4 | `escalations/notification_webpush.py`, `/me/push-*` |

**Shape of `GET /darwins/{id}/events`:** returns `{darwin_id,total,offset,limit,events}`. Paging is `limit` + `offset` (newest-first), `cursor` (events with `seq < cursor`, newest-first) or `since` (events with `seq > since`, oldest-first, for incremental polling and WS resync); `kind` is an audit-kind prefix filter, plus `neuron_id` and `tier`.

**Also shipped:** Clerk RS256 auth + token store, OpenAPI publication, Stripe/MoonPay fiat ramps, Persona/Onfido KYC, Twilio SMS inbox, Browserbase + Browser Use Cloud, ledger CSV, owner controls, harvest rules CRUD, PagerDuty sink.

The Timeline polls `GET /darwins/{id}/events?since=<head seq>` for deltas (`components/timeline/event-table.tsx`); the Anatomy page subscribes to the WebSocket stream (`lib/ws.ts`).

## Typed API client

`web/lib/api.ts` wraps `fetch` with:

- Base URL from env
- `Authorization: Bearer ${token}` on every request
- JSON parse errors surfaced as `ApiError` with `status`, `code`, `detail`
- Idempotency-Key header on `POST` actions that mutate money or lifecycle (kill, harvest, escalation respond) — matches [`../engineering/28-cross-cutting-contracts.md`](../engineering/28-cross-cutting-contracts.md)

Generate types from FastAPI OpenAPI:

```bash
# make web-codegen (or: cd web && pnpm codegen) against a running platform
openapi-typescript http://localhost:8000/openapi.json -o web/lib/api.generated.ts
```

Generated types from M0-B onward; hand-maintained DTOs are not part of the plan. The platform serves `/openapi.json` only with `--enable-docs` or `DARWIN_ENABLE_DOCS=1` (`src/darwin_platform/api.py`).

## Error handling UX

| HTTP | UI behavior |
|------|-------------|
| 401 | Redirect to sign-in; clear cached token |
| 403 | Toast: "You don't have access to this Darwin" |
| 404 | Darwin not found or deleted — return to fleet |
| 409 | Conflict (e.g. already killed) — refresh status, show message |
| 422 | Inline field errors on forms |
| 503 | Banner: platform degraded; retry with backoff |

Never retry `POST .../kill` automatically.

## Server vs client components

| Pattern | Use |
|---------|-----|
| Server Component | Static layout shells, legal copy, initial auth gate |
| Client Component | Timeline, Anatomy, charts, WebSocket, any polling |
| Route handlers (`app/api/`) | Optional BFF to hide the Clerk token behind an httpOnly cookie — **defer** unless needed; M0–M2 pass the Clerk JWT from the client for simplicity |

## CORS and cookies

FastAPI must allow the dashboard origin in dev and prod. Production auth is **Bearer in Authorization header**, not cookie sessions to the Python API, so Clerk session lives on the Next.js side only.

## Alignment with deployment

From [`../overview/07-deployment-and-infrastructure.md`](../overview/07-deployment-and-infrastructure.md):

- Cloudflare in front of Next.js (static cache, dynamic proxy).
- API on internal or public ALB; dashboard never receives organism DB connection strings.
