# Realtime, WebSockets, and caching

## WebSocket contract (shipped — `src/darwin_platform/routers/stream.py`)

Single multiplexed stream per Darwin (preferred over two sockets):

```
WSS /darwins/{darwin_id}/stream
Authorization: Bearer <token>

→ Client sends after open:
  { "type": "subscribe", "channels": ["timeline", "anatomy", "status"] }

← Server messages:
  { "type": "timeline.event", "payload": { ...Event } }
  { "type": "anatomy.patch", "payload": { "nodes": [...], "edges": [...] } }
  { "type": "status", "payload": { "lifecycle": "alive", "balance_usdc": "12.4", ... } }
  { "type": "escalation.open", "payload": { ...EscalationTicket } }
  { "type": "heartbeat", "ts": "..." }
```

Alternative split endpoints (`/timeline/stream`, `/anatomy/stream`) if multiplexing complicates backpressure — frontend `lib/ws.ts` should abstract transport either way.

> **As built (2026-10):** the single multiplexed stream shipped. On connect the server sends `snapshot.anatomy` and `snapshot.status`, then `timeline.event`, `anatomy.patch` and `status` (only when changed), `escalation.open`, `heartbeat`, and `error`. Channels are `timeline`, `anatomy`, `status`, `escalations`; `subscribe` may also carry `since` to resync from a `seq` cursor. Auth is checked once at connect — `Authorization: Bearer` or `?token=` — and the socket closes with 4401 (missing/invalid token), 4403 (not owner of this Darwin) or 4404 (unknown Darwin).

## Client behavior (`lib/ws.ts`)

| Concern | Policy |
|---------|--------|
| Connect | After REST snapshot loaded; one socket per open Darwin detail page |
| Reconnect | Exponential backoff 1s → 30s cap; resync by refetching events with `?since=<last seq>` and the anatomy snapshot |
| Auth | Browsers cannot set headers on a WebSocket upgrade, so `lib/ws.ts` passes the bearer as `?token=`; the server accepts either the header or the query parameter |
| Close on navigate away | Disconnect when leaving `/darwins/[id]` |
| Tab hidden | Keep socket; reduce UI updates (batch rAF) |
| Pause stream | Stop applying patches; still receive heartbeat to show "buffered N" |

## TanStack Query keys

```ts
['darwins']                           // fleet list
['darwin', id]                        // detail
['darwin', id, 'events', filters]     // timeline pages
['darwin', id, 'ledger']
['darwin', id, 'escalations']
['darwin', id, 'controls']
['darwin', id, 'anatomy']
```

Invalidation:

- After `POST .../kill` → invalidate all `['darwin', id]`
- After escalation respond → invalidate escalations + events
- After owner control POST → invalidate controls + anatomy (disabled neurons)

Stale times:

- Fleet list: 30s
- Darwin status: 10s when WS disconnected; infinite when WS connected (status via WS)
- Events page: append-only — never refetch full list on WS message

## Optimistic updates

Use sparingly:

- **Pause/resume:** optimistic status badge until refetch confirms.
- **Kill:** no optimism — wait for 200, then navigate to fleet with toast.
- **Escalation respond:** disable form until success; no optimistic close (legal/safety).

## Shared selection state

Use React context or Zustand store `DarwinViewStore` on `/darwins/[id]`:

```ts
selectedNeuronId: string | null
timelineFilters: { neuronIds: string[]; ... }
pauseStream: boolean
```

Anatomy and Timeline both subscribe; avoids prop drilling through layout.

*As built:* no shared store exists yet — `selectedNeuronId` is local state passed into `components/anatomy/anatomy-canvas.tsx`, and Zustand is not a dependency.

## Rate limiting UI

When API returns 429, show throttle message from `Retry-After` header. WS disconnect during platform incident: fall back to poll + banner.

## Phase 0 without WebSocket

| Feature | Fallback |
|---------|----------|
| Timeline live | Poll `GET /events` every 3s |
| Anatomy | Static from status payload; refresh on manual button |
| Escalations | Poll every 10s on inbox page |

Acceptable for soak tests and operator observation; not acceptable for Phase 2 alpha UX.
