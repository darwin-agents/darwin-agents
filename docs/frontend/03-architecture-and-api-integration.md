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
       │  (Phase 2 — spec below)               │ persistence,
       └──────────────────────────────────────►│ read-only DB
```

1. User signs in via Clerk/WorkOS (Phase 2) or pastes a dev token (Phase 0).
2. Next.js server or client obtains a **platform bearer token** with `scope=owner` and `owned_darwins=[...]`.
3. All data mutations and reads use that token against `NEXT_PUBLIC_API_BASE_URL`.
4. Realtime views open a WebSocket after the REST snapshot loads (see [`07-realtime-websockets-and-caching.md`](07-realtime-websockets-and-caching.md)).

## API surface the UI depends on

Implemented today in `src/darwin_platform/api.py` (representative list; OpenAPI is source of truth once published):

| Area | Methods | Purpose |
|------|---------|---------|
| Lifecycle | `POST/GET /darwins`, `GET /darwins/{id}`, pause, resume, kill, force reproduce | Fleet and per-Darwin status |
| Events | `GET /darwins/{id}/events` | Timeline (paginated) |
| Ledger | `GET /darwins/{id}/ledger` | Money trail |
| Escalations | `GET`, `GET/{id}`, `POST .../respond` | Inbox and resolution |
| Lineage | `GET /darwins/{id}/lineage`, `/lineage/tree` | Tree visualization |
| Owner controls | `POST /controls/*`, `GET /controls` | Spend lock, blocks, disabled neurons |
| Harvest | `POST /harvest`, harvest rules CRUD | Profit extraction |
| Billing | `GET /darwins/{id}/billing` | Cost breakdown |
| Vault | `GET/POST/DELETE /darwins/{id}/vault` | Credential browser |
| Resources | `GET/DELETE /darwins/{id}/resources` | Provisioned DBs, buckets |
| Alerts | `GET /darwins/{id}/alerts` | Graded response badges |
| Browser | `GET /darwins/{id}/browser/sessions` | Live session list for takeover |
| Fiat | `POST /platform/fiat/*` | Deposits (platform-scoped) |
| Health | `GET /healthz`, `/readyz` | Shell connectivity indicator |

**Not yet implemented (frontend spec, backend TODO):**

| Endpoint | Consumer |
|----------|----------|
| `GET /darwins/{id}/anatomy` | Anatomy snapshot (neurons, edges, active nodes) |
| `WS /darwins/{id}/stream` or split `.../anatomy/stream` + `.../timeline/stream` | Live canvas updates |
| `GET /darwins/{id}/data` + `/data/{table}` | Data browser (Phase 2) |

**Partial gaps in existing endpoints:**

- `GET /darwins/{id}/events` is **offset-paginated with no server-side filters** (`limit` + `offset` only). Cursor pagination, `neuron_id` / `category` / `tier` filters, and an incremental `?since=` param are backend tasks — see [`10-phased-delivery.md`](10-phased-delivery.md) register #1 and #10. Until then the Timeline filters client-side and polling refetches page 0.
- There is no aggregate `GET /escalations`; the inbox fans out per Darwin (`GET /darwins/{id}/escalations`).

Phase 0 Timeline can poll `GET /darwins/{id}/events` every 2–5s until the WebSocket exists.

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

Hand-maintained DTOs are acceptable for Phase 0 only; generated types before Phase 2 alpha.

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
| Route handlers (`app/api/`) | Optional BFF to hide token from browser — **defer** until we need to hide secrets; Phase 0 passes bearer from client for simplicity |

## CORS and cookies

FastAPI must allow the dashboard origin in dev and prod. Production auth is **Bearer in Authorization header**, not cookie sessions to the Python API, so Clerk session lives on the Next.js side only.

## Alignment with deployment

From [`../overview/07-deployment-and-infrastructure.md`](../overview/07-deployment-and-infrastructure.md):

- Cloudflare in front of Next.js (static cache, dynamic proxy).
- API on internal or public ALB; dashboard never receives organism DB connection strings.
