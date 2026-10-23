# Tech stack and repository layout

Aligned with [`../engineering/11-tech-stack-at-a-glance.md`](../engineering/11-tech-stack-at-a-glance.md).

## Core stack

| Layer | Choice | Notes |
|-------|--------|-------|
| Framework | **Next.js 15** (App Router) | SSR for marketing later; dashboard routes mostly client components for live data |
| Language | **TypeScript** (strict) | Shared types generated from OpenAPI where possible |
| Styling | **Tailwind CSS** + **shadcn/ui** | Radix primitives; copy-paste components, no heavy MUI-style runtime |
| Charts | **Recharts** | Treasury, fitness, spend velocity — routine time series |
| Custom viz | Hand-rolled **SVG** | Anatomy canvas in `components/anatomy/anatomy-canvas.tsx`; lineage is a list view. d3 / React Flow were planned but not adopted |
| Data fetching | **TanStack Query** | Server state, polling, cache invalidation on mutation |
| Forms | Controlled React state | react-hook-form was planned but is not a dependency; `zod` is installed but not yet imported |
| Realtime | Native **WebSocket** (browser) | Thin wrapper in `lib/ws.ts`; reconnect with backoff |
| Auth (prod) | **Clerk** | Wired on backend (`security/clerk.py`); RS256 JWTs verified against the issuer's JWKS |
| Mobile | **PWA** (W3C Web Push + VAPID) | Milestone M4, folded into `web/`: `public/manifest.webmanifest`, `public/sw.js`, `/notifications` opt-in page; no separate Expo app |

## Deployment targets

- **Phase 0–1:** Vercel preview or `next dev` against local FastAPI (`darwin-platform serve`).
- **Phase 2:** Vercel in production (`infra/README.md`); env-specific `NEXT_PUBLIC_API_BASE_URL`. Locally it runs under Compose (`darwin-web`, :3000) or `make web-dev`.

No Darwin code runs in the frontend bundle. Environment variables are only platform URLs, auth keys, and feature flags — never Darwin vault secrets or wallet keys.

## Repository layout

```
web/
  app/
    layout.tsx · globals.css
    sign-in/[[...rest]]/ · sign-up/[[...rest]]/   # Clerk
    privacy/ · terms/
    (dashboard)/
      page.tsx                    # Fleet home
      darwins/new/                # Spawn wizard
      darwins/[id]/               # Per-Darwin shell + tabs:
        anatomy/ timeline/ summary/ lineage/ billing/ payments/
        preapprovals/ vault/ data/ resources/ proposals/
        forensics/ operator/ settings/
      escalations/ freelancers/ notifications/ onboarding/ operator/ tokens/
  components/  anatomy/ darwin/ layout/ operator/ pwa/ timeline/ ui/
  lib/         api.ts · api.generated.ts · ws.ts · auth.ts · auth-mode.ts
               push.ts · push-sync.ts · queryKeys.ts · types.ts · format.ts
  providers/   auth-provider · dev-auth-provider · query-provider
  public/      manifest.webmanifest · sw.js · icons/
  playwright/  clerk/ · dev-token/ · live/   (E2E)
  test/        Vitest setup
  middleware.ts · next.config.ts · tailwind.config.ts · package.json
```

The Python monorepo stays under `src/`. CI runs `pnpm lint`, `pnpm typecheck`, `pnpm test`, `pnpm build` and Playwright in `web/` (`.github/workflows/web.yml`) independently of `pytest`. There is no `mobile/` package — the escalation companion is the PWA above.

## Package manager

Use **pnpm**. A single `web/package.json` is enough; extract a shared `packages/api-client` only if a second client appears.

## Visual language

- **Metaphor-forward, not gimmicky:** organism map and timeline should feel like instrumentation, not a game UI.
- **Status semantics:** alive (green pulse), paused (amber), quarantined (orange lock), dead (muted, no pulse), killed (red, strikethrough in history).
- **Density:** power users watch Timeline for hours; default to compact rows with expand-in-place detail.
- **Accessibility:** keyboard navigation on Timeline; Anatomy nodes focusable; WCAG AA contrast on shadcn defaults.

## Local development

```bash
# Terminal 1 — platform API
darwin-platform serve --host 0.0.0.0 --port 8000

# Terminal 2 — dashboard
cd web && pnpm dev     # or: make web-dev
```

`NEXT_PUBLIC_API_BASE_URL=http://localhost:8000` plus either a dev bearer token (pasted in the UI or `NEXT_PUBLIC_DEV_BEARER_TOKEN`, from `--auth-tokens`) or Clerk keys (`NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`). See [`08-auth-session-and-security.md`](08-auth-session-and-security.md).
