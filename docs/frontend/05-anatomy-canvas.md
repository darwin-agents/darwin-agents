# Anatomy canvas

The **Anatomy** view answers: *what is this organism structurally right now?* It is a live map of the orchestrator, registered neurons, and in-flight work — not a marketing illustration.

## Data sources

| Field | Source | Refresh |
|-------|--------|---------|
| Neuron registry | Soma manifest / platform snapshot of enabled neurons | On mutation, reproduction, owner disable |
| Graph topology | LangGraph state: nodes = orchestrator steps + active neuron dispatches | WebSocket or 2s poll |
| Activity heat | Per-neuron dispatch count in last N minutes (platform metrics or Honeycomb rollup) | WebSocket |
| DNA summary | Layer list, generation, constitution version (read-only) | On reproduce |
| Current strategy | Active strategy id + label from runtime snapshot | WS |

Planned REST aggregate (backend to implement):

```json
GET /darwins/{id}/anatomy
{
  "orchestrator": { "id": "orchestrator", "label": "Orchestrator", "state": "plan" },
  "neurons": [
    { "id": "content.writer", "label": "content.writer", "status": "idle|active|disabled|error", "last_dispatch_at": "..." }
  ],
  "edges": [
    { "from": "orchestrator", "to": "content.writer", "kind": "dispatch", "active": true }
  ],
  "heatmap": { "content.writer": 0.82, "browser.operator": 0.1 },
  "dna": { "generation": 2, "layers": ["L0-v1.0.0", "L1-..."] }
}
```

Until that endpoint exists, the client composes from `GET /darwins/{id}`, `GET /controls`, and platform-internal fields returned on the status payload if extended.

## Visualization stack

**Primary:** `@xyflow/react` (React Flow) with a custom node type per neuron category.

**Alternative considered:** raw d3 force simulation — more control, higher maintenance. React Flow wins for pan/zoom, minimap, and edge routing out of the box.

Layout modes:

1. **Radial (default)** — orchestrator fixed center; neurons on a ring; category-based grouping (content, browser, payments, infra).
2. **Tree (lineage context)** — when opened from Lineage view, show parent/child Darwin nodes above/below (separate mode flag).

## Node visual design

| State | Appearance |
|-------|------------|
| `idle` | Soft outline, no glow |
| `active` | Pulse animation on border; optional particle on edge from orchestrator |
| `disabled` (owner) | Gray, dashed border, lock icon |
| `error` | Red border; tooltip with last error from Timeline |
| `dying` | Fade-out over 2s when neuron removed from soma |

Node label: neuron id (monospace). Tooltip: last success/failure, 24h cost attributable.

## Edges

- **Dispatch edge:** orchestrator → neuron, animated dash while `active: true`.
- **Data flow (optional Phase 2+):** neuron → neuron when LangGraph state shows handoff (rare in Layer 0).

Edge thickness scales with recent event count (heatmap).

## Interactions

| Action | Behavior |
|--------|----------|
| Click neuron | Select; emit `anatomy:select` to Timeline filter |
| Double-click | Open Timeline filtered + scroll to latest event for neuron |
| Hover | Highlight incident edges; show mini stats |
| Pan / zoom | Persist viewport in sessionStorage per Darwin |
| Minimap | Bottom-right when >8 nodes |

## Animation requirements

- New neuron after mutation: grow-in (scale 0 → 1, 300ms ease).
- Removed neuron: fade + collapse.
- Re-layout: smooth transition (React Flow `fitView` debounced, avoid jarring jumps every WS tick).

**Pause stream** toggle (shared with Timeline): freeze layout updates but keep last frame for study.

## Performance

- Cap displayed neurons at 64; beyond that, cluster by category with expand.
- Throttle WS graph updates to max 10 Hz; batch node status changes.
- Use `memo` on custom node components.

## Accessibility

- Nodes are focusable buttons; Enter selects.
- Live region announces "content.writer now active" on status transitions (respect reduced-motion).

## Browser session overlay (Phase 2)

When `browser.operator` is active, show a chip "Live browser" linking to Browserbase takeover flow — see [`09-owner-controls-escalations-mobile.md`](09-owner-controls-escalations-mobile.md).

## Testing

- Storybook stories for node states.
- Visual regression on golden graph fixtures (JSON → screenshot).
- Integration: mock WS payload → expect Timeline filter callback.

Cross-reference: [`../engineering/19-system-diagrams.md`](../engineering/19-system-diagrams.md) §5.
