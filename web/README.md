# Darwin web dashboard

Next.js 15 + TypeScript + Tailwind v4. See `../docs/frontend/` for the design
docs and `../docs/frontend/10-phased-delivery.md` for the milestone-by-milestone
build plan.

## Status

- **M0 Foundations** — done. Scaffold, design tokens, typed API client, dev
  auth shim, TanStack Query provider, CI workflow.
- **M1 Read-only fleet** — first cut shipped. Fleet home, per-Darwin shell with
  events table + treasury strip + escalations sidebar, lineage indented list,
  billing screen with treasury chart + ledger CSV download, spawn wizard, kill
  modal with type-the-id confirmation + Idempotency-Key.
- **M2 Owner controls** — partial. Spend lock, reproduction block, and
  force-reproduce wired. Disabled-neuron/category editors, harvest rules CRUD,
  escalation respond drawer pending.
- **M3 Live canvases** — gated on backend register #1–#4 (events filters, anatomy
  snapshot, WS stream, data browser).
- **M4 Mobile** — not started; needs FCM sink (register #7).

## Develop

```bash
# Terminal 1 — platform API
darwin-platform serve --host 0.0.0.0 --port 8000

# Terminal 2 — dashboard
cd web
pnpm install
cp .env.example .env.local   # set NEXT_PUBLIC_DEV_BEARER_TOKEN if needed
pnpm dev
```

The dev auth gate prompts for a bearer token on first load; paste one from the
platform's in-memory `TokenStore` (or set `NEXT_PUBLIC_DEV_BEARER_TOKEN` and
skip the prompt entirely).

## Scripts

```
pnpm dev          # next dev
pnpm build        # next build
pnpm lint         # next lint (eslint flat config)
pnpm typecheck    # tsc --noEmit
pnpm test         # vitest run
pnpm codegen      # openapi-typescript -> lib/api.generated.ts
```

`pnpm codegen` regenerates `lib/api.generated.ts` from the live FastAPI
`/openapi.json`. Until that file exists, `lib/types.ts` carries the
hand-written DTOs the UI consumes.

## Layout

```
app/
  layout.tsx              # root: providers + body shell
  (dashboard)/
    layout.tsx            # sidebar + API banner
    page.tsx              # /          fleet home
    darwins/
      new/page.tsx        # /darwins/new
      [id]/
        page.tsx          # /darwins/[id]          shell
        lineage/page.tsx
        billing/page.tsx
        settings/page.tsx
    escalations/page.tsx  # cross-Darwin inbox
    _tokens/page.tsx      # dev-only design-token preview
components/
  darwin/                 # status badge, kill dialog, header, treasury strip, …
  timeline/               # event table + row
  ui/                     # shadcn-style primitives (button, card, dialog, …)
  layout/                 # sidebar, API banner
lib/
  api.ts                  # typed fetch wrapper + endpoint helpers
  auth.ts                 # dev token store (Clerk shim later)
  cn.ts                   # tailwind-merge + clsx
  format.ts               # money / time / id formatters
  queryKeys.ts            # canonical TanStack Query keys
  types.ts                # hand-maintained DTOs (until codegen)
providers/
  query-provider.tsx      # TanStack Query + devtools
  dev-auth-provider.tsx   # paste-token gate (Clerk replaces in prod)
```

## Notes

- **Trust boundary.** The browser only talks to the platform REST/WS. Never
  embed Darwin runtime code or vault values here.
- **CSP.** `next.config.ts` sets a baseline policy that allowlists
  Browserbase + Browser Use Cloud domains in `frame-src` (M2 captcha takeover).
- **Idempotency.** `lib/api.ts` auto-adds `Idempotency-Key` to every mutating
  POST; `kill` is generated per-modal-open so retrying the button never produces
  a second order.
- **Polling.** Timeline polls page 0 every 3s and dedupes by `event_id`.
  WebSocket replaces polling in M3.
