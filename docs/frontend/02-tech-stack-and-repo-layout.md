# Tech stack and repository layout

Aligned with [`../engineering/11-tech-stack-at-a-glance.md`](../engineering/11-tech-stack-at-a-glance.md).

## Core stack

| Layer | Choice | Notes |
|-------|--------|-------|
| Framework | **Next.js 15** (App Router) | SSR for marketing later; dashboard routes mostly client components for live data |
| Language | **TypeScript** (strict) | Shared types generated from OpenAPI where possible |
| Styling | **Tailwind CSS** + **shadcn/ui** | Radix primitives; copy-paste components, no heavy MUI-style runtime |
| Charts | **Recharts** | Treasury, fitness, spend velocity — routine time series |
| Custom viz | **d3** + **@xyflow/react** (React Flow) | Lineage tree (d3); Anatomy graph (React Flow force or layered layout) |
| Data fetching | **TanStack Query** | Server state, polling, cache invalidation on mutation |
| Forms | **react-hook-form** + **zod** | Escalation responses, harvest rules, spawn wizard |
| Realtime | Native **WebSocket** (browser) | Thin wrapper in `lib/ws.ts`; reconnect with backoff |
| Auth (prod) | **Clerk** or **WorkOS** | Pick at Phase 2 kickoff; org/multi-tenant ready |
| Mobile | **Expo** (React Native) | Phase 2; shares types and API client with `web/` |

## Deployment targets

- **Phase 0–1:** Vercel preview or `next dev` against local FastAPI (`darwin-platform serve`).
- **Phase 2:** Vercel or container on EKS behind Cloudflare — same artifact; env-specific `NEXT_PUBLIC_API_BASE_URL`.

No Darwin code runs in the frontend bundle. Environment variables are only platform URLs, auth keys, and feature flags — never Darwin vault secrets or wallet keys.

## Repository layout (planned)

```
web/                              # Next.js dashboard (new)
  app/
    (auth)/                       # Clerk/WorkOS callback routes
    (dashboard)/
      page.tsx                    # Fleet home — Darwin list
      darwins/
        new/page.tsx              # Spawn wizard
        [id]/
          page.tsx                # Per-Darwin shell: Anatomy + Timeline + side panels
          timeline/page.tsx       # Full-width Timeline (optional deep link)
          anatomy/page.tsx        # Full-width Anatomy
          vault/page.tsx
          data/[[...table]]/page.tsx
          lineage/page.tsx
          billing/page.tsx
          settings/page.tsx
      escalations/page.tsx        # Cross-Darwin inbox
    layout.tsx
    globals.css
  components/
    anatomy/                      # Canvas, nodes, edges, minimap
    timeline/                     # Virtualized feed, event cards
    darwin/                       # Status badges, treasury strip, controls
    ui/                           # shadcn
  lib/
    api.ts                        # Typed fetch wrapper + generated types
    ws.ts                         # WebSocket client
    auth.ts                       # Session → platform bearer token
    format.ts                     # Money, duration, relative time
  openapi/                        # Generated from FastAPI (optional script in Makefile)
  public/
  package.json
  tsconfig.json
  next.config.ts
  tailwind.config.ts

mobile/                           # Expo escalation app (Phase 2)
  app/
  src/
    api.ts                        # Re-export or symlink shared client patterns
    notifications.ts              # FCM registration
  app.json
```

The Python monorepo stays under `src/`. `web/` and `mobile/` are sibling packages; CI runs `pnpm lint`, `pnpm test`, and `pnpm build` in `web/` independently of `pytest`.

## Package manager

Use **pnpm** workspaces if we later extract `packages/api-client` shared by web and mobile; until mobile exists, a single `web/package.json` is enough.

## Visual language

- **Metaphor-forward, not gimmicky:** organism map and timeline should feel like instrumentation, not a game UI.
- **Status semantics:** alive (green pulse), paused (amber), quarantined (orange lock), dead (muted, no pulse), killed (red, strikethrough in history).
- **Density:** power users watch Timeline for hours; default to compact rows with expand-in-place detail.
- **Accessibility:** keyboard navigation on Timeline; Anatomy nodes focusable; WCAG AA contrast on shadcn defaults.

## Local development

```bash
# Terminal 1 — platform API
darwin-platform serve --host 0.0.0.0 --port 8000

# Terminal 2 — dashboard (once web/ exists)
cd web && pnpm dev
```

`NEXT_PUBLIC_API_BASE_URL=http://localhost:8000` and a dev bearer token from platform seed auth (until Clerk lands). See [`08-auth-session-and-security.md`](08-auth-session-and-security.md).
