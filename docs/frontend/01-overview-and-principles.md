# Overview and principles

## What the frontend is for

The dashboard is how a human **owns** a Darwin without operating it. The product promise from [`../overview/01-vision-and-metaphor.md`](../overview/01-vision-and-metaphor.md) boils down to four jobs the UI must make easy:

1. **Watch** — see what the organism is (Anatomy) and what it is doing right now (Timeline).
2. **Fund** — deposit, set caps, configure harvest rules.
3. **Help** — resolve escalations (captcha, KYC, spend approval, information requests).
4. **Stop** — pause, throttle indirectly via settings, or kill with clear consequences.

The mobile client (Phase 2) is intentionally narrow: push on escalation, inline approve/deny, quick status. A full mobile product is out of scope until Phase 4+.

## The trust boundary

```
User browser  →  Next.js (dashboard)  →  Platform API  →  Supervisor / read-only DB roles
                                              ↕
                                         (never) Darwin runtime
```

- The Darwin does **not** render the dashboard and does **not** write dashboard-facing narrative. Views are built from the audit log, platform metrics, LangGraph state snapshots, and read-only organism DB access — see [`../engineering/10-alignment-and-anti-wireheading.md`](../engineering/10-alignment-and-anti-wireheading.md).
- Timeline entries are **raw events**, not summaries produced by the agent.
- Anatomy reflects the **platform's neuron registry and task graph**, not the Darwin's self-description.

If we ever add "explain this to me" copy in the UI, it must be clearly labeled as assistant commentary on top of primary sources, with links to the underlying event IDs.

## Design principles (mapped to UI)

Checked against [`../overview/02-design-principles.md`](../overview/02-design-principles.md):

| Principle | UI implication |
|-----------|----------------|
| **P3 — Owner sovereignty** | Kill, pause, spend lock, and cap tightening are always one or two clicks away; destructive actions need explicit confirmation. |
| **P4 — Transparency** | Timeline is complete and filterable; ledger and cost breakdowns reconcile to API numbers; no hidden fees in copy. |
| **P5 — Fail closed** | If the API is unreachable, show stale data with a banner — do not invent status. Escalation actions disable when session is invalid. |
| **P6 — Economic skin in the game** | Treasury chart and burn rate are prominent on the per-Darwin dashboard; death-by-bankruptcy is explained before spawn. |
| **P8 — Escalation, not micromanagement** | Default view is observability; controls are available but not the homepage. |

## The two centerpiece canvases

Every per-Darwin dashboard is anchored on two linked views (see [`../engineering/08-platform-control-plane.md`](../engineering/08-platform-control-plane.md)):

| Canvas | Question it answers | Primary data |
|--------|---------------------|--------------|
| **Anatomy** | What is this organism? | Neuron registry, LangGraph in-flight graph, activity heatmap, DNA layer summary |
| **Timeline** | What is it doing? | Hash-chained event log, LLM call records, payments, escalations, mutations |

**Linked interaction (required in Phase 2):**

- Click neuron on Anatomy → Timeline filters to that `neuron_id`.
- Click Timeline row → Anatomy highlights neuron + edge involved in that dispatch.
- Optional: shared time cursor when scrubbing Timeline (Anatomy heatmap follows).

## Surrounding screens

Beyond the canvases, the app includes fleet home, vault, data browser, escalation inbox, lineage, wallet/deposits, billing, and settings — enumerated in [`04-screens-and-routing.md`](04-screens-and-routing.md).

## What we are not building (yet)

- Internationalization (English only at launch).
- Team / org accounts (one user, one wallet, N Darwins).
- User-authored custom neurons or strategy marketplace.
- Public marketing site (separate repo or route group later).
- Operator console (internal tooling uses separate WebAuthn-protected admin UI; not part of `web/` owner app).
