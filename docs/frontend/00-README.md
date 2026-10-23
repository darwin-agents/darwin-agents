# Darwin — Frontend documentation

The user-facing web dashboard and its installable escalation companion (PWA). Read [`../overview/`](../overview/) for the product idea and [`../engineering/`](../engineering/) for the platform API and control plane; this folder is the *how we build the UI*.

The frontend never talks to a Darwin directly. Every screen goes through the **Darwin Platform API** (FastAPI), which is the only component allowed to read organism state, flip lifecycle flags, and route escalations. That boundary is non-negotiable — see [`01-overview-and-principles.md`](01-overview-and-principles.md).

## Status

**Shipped (2026-10).** The dashboard lives in [`web/`](../../web/) — Next.js 15 App Router, Clerk auth (`@clerk/nextjs`, with a dev-token mode), TanStack Query, Recharts, Vitest and Playwright E2E in `.github/workflows/web.yml`. Milestones M0–M3 of [`10-phased-delivery.md`](10-phased-delivery.md) are delivered, including the WebSocket stream, Anatomy and Timeline canvases and the data browser. M4 shipped as an installable **PWA** with W3C Web Push (`web/public/manifest.webmanifest`, `web/public/sw.js`, `/notifications` opt-in page) rather than a separate Expo app, so there is no `mobile/` directory. Backend status for every surface is in [`../engineering/BACKLOG.md`](../engineering/BACKLOG.md).

## Reading order

| # | Doc | Contents |
|---|-----|----------|
| 01 | [`01-overview-and-principles.md`](01-overview-and-principles.md) | Product role of the UI, trust model, design constraints |
| 02 | [`02-tech-stack-and-repo-layout.md`](02-tech-stack-and-repo-layout.md) | Next.js 15, Tailwind, shadcn/ui, charts, monorepo layout |
| 03 | [`03-architecture-and-api-integration.md`](03-architecture-and-api-integration.md) | Request flow, auth, typed client, env config |
| 04 | [`04-screens-and-routing.md`](04-screens-and-routing.md) | App routes, screens, API endpoint map |
| 05 | [`05-anatomy-canvas.md`](05-anatomy-canvas.md) | Organism map: data model, layout, animation, linking |
| 06 | [`06-timeline-canvas.md`](06-timeline-canvas.md) | Event feed: virtualization, filters, event types |
| 07 | [`07-realtime-websockets-and-caching.md`](07-realtime-websockets-and-caching.md) | Live updates, pause stream, client state |
| 08 | [`08-auth-session-and-security.md`](08-auth-session-and-security.md) | Clerk (wired on backend), bearer scopes, CSP, secrets |
| 09 | [`09-owner-controls-escalations-mobile.md`](09-owner-controls-escalations-mobile.md) | Controls UI, inbox, Browserbase takeover, PWA push companion |
| 10 | [`10-phased-delivery.md`](10-phased-delivery.md) | Milestone-by-milestone build plan (M0 Foundations → M4 Mobile/PWA) with delivery status, backend work register, risk register, CI gates |

## Related engineering docs

- [`../engineering/BACKLOG.md`](../engineering/BACKLOG.md) — ground truth for what is done / partial / missing across the platform; the source for the "what's unblocked" list in [`10`](10-phased-delivery.md)
- [`../engineering/08-platform-control-plane.md`](../engineering/08-platform-control-plane.md) — supervisor API, web app surfaces, billing
- [`../engineering/09-kill-switches-and-owner-controls.md`](../engineering/09-kill-switches-and-owner-controls.md) — owner actions the UI must expose
- [`../engineering/11-tech-stack-at-a-glance.md`](../engineering/11-tech-stack-at-a-glance.md) — stack choices (Next.js, Tailwind, Recharts, d3)
- [`../engineering/19-system-diagrams.md`](../engineering/19-system-diagrams.md) — diagram §5: Anatomy/Timeline data flow
- [`../engineering/20-phase-zero-runtime-build-plan.md`](../engineering/20-phase-zero-runtime-build-plan.md) — minimal dashboard acceptance criteria
- [`../engineering/22-phase-two-multi-tenant-build-plan.md`](../engineering/22-phase-two-multi-tenant-build-plan.md) — full dashboard + mobile module map
- [`../overview/06-roadmap-and-phases.md`](../overview/06-roadmap-and-phases.md) — when the UI ships per phase
