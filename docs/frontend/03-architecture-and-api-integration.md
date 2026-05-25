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
       │  (M3 — spec below, backend TODO)      │ persistence,
       └──────────────────────────────────────►│ read-only DB
```

1. User signs in via Clerk (production) or pastes a dev token (local dev only).
2. Clerk's RS256 JWT is sent directly as `Authorization: Bearer` — the backend's `ClerkTokenStore` (`security/clerk.py`) maps claims → `scope=owner` and `owned_darwins=[...]`. No exchange step.
3. All data mutations and reads use that token against `NEXT_PUBLIC_API_BASE_URL`.
4. Realtime views open a WebSocket after the REST snapshot loads (see [`07-realtime-websockets-and-caching.md`](07-realtime-websockets-and-caching.md)) — currently TODO on the backend; see milestone M3.

## API surface the UI depends on

Implemented today in `src/darwin_platform/api.py` (representative list; OpenAPI is source of truth once published):

| Area | Methods | Purpose |
|------|---------|---------|
| Lifecycle | `POST/GET /darwins`, `GET /darwins/{id}`, pause, resume, kill, force reproduce | Fleet and per-Darwin status |
| Events | `GET /darwins/{id}/events` (offset-paged) | Timeline |
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

**Not yet implemented (frontend spec, backend TODO — see [`10-phased-delivery.md`](10-phased-delivery.md) "Backend API work register"):**

| # | Endpoint | Consumer |
|---|----------|----------|
| 1 | `GET /darwins/{id}/events?cursor=&neuron_id=&category=&tier=` (filtering + cursor) | Timeline M3-B |
| 2 | `GET /darwins/{id}/anatomy` | Anatomy snapshot M3-A |
| 3 | `WSS /darwins/{id}/stream` (multiplexed `timeline` / `anatomy` / `status`) | Live canvases M3-C |
| 4 | `GET /darwins/{id}/data` + `/data/{table}` | Data browser M3-E |
| 5 | `GET /escalations` (owner-scoped aggregate, optional) | Inbox M2-B |
| 6 | `?since={event_id}` on events endpoint | Polling + WS resync |
| 7 | FCM notification sink + push payload | Mobile M4 |

**Current shape of `GET /darwins/{id}/events`:** offset-paginated, `limit` + `offset` only, returns `{darwin_id,total,offset,limit,events}`. Until #1 / #6 land, the Timeline filters client-side over a loaded window and polling refetches page 0 with `event_id` dedupe.

**Already shipped — no longer blockers:** Clerk RS256 auth + token store, OpenAPI publication, Stripe/MoonPay fiat ramps, Persona/Onfido KYC, Twilio SMS inbox, Browserbase + Browser Use Cloud, ledger CSV, owner controls, harvest rules CRUD, PagerDuty sink.

Phase M1 Timeline polls `GET /darwins/{id}/events` every 3s until the WebSocket exists.

## Typed API client

`web/lib/api.ts` wraps `fetch` with:

- Base URL from env
- `Authorization: Bearer ${token}` on every request
- JSON parse errors surfaced as `ApiError` with `status`, `code`, `detail`
- Idempotency-Key header on `POST` actions that mutate money or lifecycle (kill, harvest, escalation respond) — matches [`../engineering/28-cross-cutting-contracts.md`](../engineering/28-cross-cutting-contracts.md)

Generate types from FastAPI OpenAPI:

```bash
# Planned Makefile target
openapi-typescript http://localhost:8000/openapi.json -o web/lib/api.generated.ts
```

Generated types from M0-B onward (the FastAPI app already serves `/openapi.json`); hand-maintained DTOs are not part of the plan.

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
