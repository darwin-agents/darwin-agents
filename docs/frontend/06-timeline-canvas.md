# Timeline canvas

The **Timeline** answers: *what is the Darwin doing, in order, with evidence?* It is a human-oriented trace — not a developer-only log tail.

## Data source

**Today:** `GET /darwins/{id}/events?limit=&offset=` — offset-based pagination only. The endpoint returns `{darwin_id, total, offset, limit, events}` and does **not** support `neuron_id` / `category` / `tier` filters or a `since` cursor. See [`10-phased-delivery.md`](10-phased-delivery.md) backend register #1 and #10.

**Phase 2 target:** `GET /darwins/{id}/events?cursor=&neuron_id=&category=&tier=` — cursor pagination + server-side filtering.

Response is a paginated list of hash-chained audit events (see [`../engineering/23-data-model-reference.md`](../engineering/23-data-model-reference.md) for event shape). The UI treats the platform as authoritative; never merge Darwin-supplied prose.

## Event categories (display)

Map `event_type` / `category` from API to row templates:

| Category | Icon | Row summary | Expandable detail |
|----------|------|-------------|-------------------|
| `orchestrator` | brain | Plan / reflect / check loop | Node name, decision summary from structured fields only |
| `neuron_dispatch` | cell | `{neuron}` started/completed | Input/output refs, duration, success |
| `llm_call` | spark | `{tier}` → `{model}` · `{latency}ms` · `${cost}` | Prompt hash id, token counts (no raw prompt in UI by default) |
| `payment` | dollar | `{direction}` `{amount}` USDC | Counterparty, tx hash link, policy check result |
| `escalation` | flag | Escalation opened/resolved | Ticket class, deadline, owner response |
| `mutation` | dna | Patch proposed/admitted/rejected | Sandbox id, fitness delta |
| `reproduction` | branch | Child `{child_id}` spawned | Parent fitness, lesson count |
| `owner_action` | user | Owner set spend lock | Control payload |
| `platform` | shield | Throttle, quarantine, kill | Reason code |
| `death` | skull | Death sequence | Final balance, cause |

Unknown types render as generic JSON expander — never hidden.

## Layout

**Phase 0:** Vertical table, newest first, infinite scroll up (load older on scroll top).

**Phase 2:** Horizontal timeline optional — time on X axis, swimlanes per neuron category. Default remains vertical for readability on mobile widths.

Each row:

```
[time] [icon] [summary]  [cost chip]  [chevron]
         └─ expanded: key-value detail + link to external (tx, session)
```

## Filtering

Toolbar:

- Neuron multi-select (synced from Anatomy selection)
- Tier: fast / reasoning / coding / embed
- Category checkboxes
- Time range: 1h / 24h / 7d / all
- Search: full-text on `summary` and `event_id`

**Phase 0/1 — client-side only.** The events endpoint has no server filters yet, so every toolbar control narrows the *already-loaded window* (the offset pages fetched so far). This is acceptable for operator soak-watching but will miss matches outside the window.

**Phase 2 — server-side.** Once backend register #1 lands, neuron / category / tier become query params and filtering covers the full history.

URL query params encode filters for shareable links either way: `/darwins/[id]/timeline?neuron=content.writer`.

## Virtualization

Use `@tanstack/react-virtual` — thousands of events per day are expected.

- Row height estimate 56px collapsed, dynamic on expand.
- Sticky "jump to live" button when scrolled away from newest.

## Live updates

On WebSocket `timeline.event`:

1. Prepend row if newest-first.
2. If user is at live edge, auto-scroll; else increment "N new events" chip.
3. Respect pause-stream: buffer events, apply on resume.

Polling fallback (Phase 0): there is no `?since=` param yet, so refetch page 0 (`offset=0`) every 3s when the tab is visible (`document.visibilityState`) and deduplicate against rendered rows by `event_id`. An incremental `?since={event_id}` param is backend register #10 — adopt it for Phase 1 once available.

## Cost and treasury strip

Above Timeline, show rolling:

- Last hour LLM spend (sum `llm_call` costs)
- Last hour payments net
- Link to full ledger and billing tabs

Numbers must match `GET /billing` within rounding — if mismatch, show warning badge.

## Expand row / raw mode

Default expand shows structured fields. "Raw JSON" tab for support — copies redacted payload (strip vault paths).

## Export

Phase 2: "Download visible events as JSONL" for accountant / debugging.

Ledger CSV export is separate and **already available** — `GET /darwins/{id}/ledger.csv` returns a jurisdiction-neutral CSV with a `Content-Disposition` attachment header. Wire it from the billing screen ([`04-screens-and-routing.md`](04-screens-and-routing.md)).

## Performance

- Fetch pages of 50–100 events.
- Deduplicate by `event_id` on WS + poll overlap.
- Do not render more than 500 expanded rows without virtualization.

## Testing

- Fixture files in `web/tests/fixtures/events/*.json`
- Playwright: load Darwin page, assert row count, filter by neuron, expand payment row shows hash

Cross-reference: [`../engineering/10-alignment-and-anti-wireheading.md`](../engineering/10-alignment-and-anti-wireheading.md) — dashboard reads audit log only.
