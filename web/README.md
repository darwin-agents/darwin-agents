# Darwin web dashboard

Next.js 15 + TypeScript + Tailwind v4. See `../docs/frontend/` for the design
docs and `../docs/frontend/10-phased-delivery.md` for the milestone-by-milestone
build plan.

## Status

The v1 owner dashboard: fleet, per-Darwin shell (timeline, treasury, money
card with storefront URL, payout address and harvest, alerts), escalations
inbox, lineage, billing + ledger CSV, onboarding (KYC → push → spawn →
fund), MoonPay deposit, and `/terms` + `/privacy` (drafts pending counsel).
Response types are generated from the API (`pnpm codegen`); `lib/types.ts`
only aliases them.

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

`pnpm codegen` regenerates `lib/api.generated.ts` from the FastAPI
`/openapi.json` (`make web-codegen` from the repo root). Rerun it whenever an
API model changes; `lib/types.ts` aliases the generated types.

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
  types.ts                # aliases of the generated API types
providers/
  query-provider.tsx      # TanStack Query + devtools
  dev-auth-provider.tsx   # paste-token gate (Clerk replaces in prod)
```

## Deploy (Vercel)

One Vercel project, **Root Directory `web/`**, framework Next.js (pnpm is
picked up from the lockfile). Production tracks `main` and points at the prod
API; previews point at staging.

| Variable | Production | Preview |
| --- | --- | --- |
| `NEXT_PUBLIC_API_BASE_URL` | `https://<prod api domain>` | `https://<staging api domain>` |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Clerk **production** instance `pk_live_…` | staging instance `pk_test_…` |
| `CLERK_SECRET_KEY` | `sk_live_…` (sensitive) | `sk_test_…` |
| `NEXT_PUBLIC_CLERK_SIGN_IN_URL` / `_SIGN_UP_URL` | `/sign-in` / `/sign-up` | same |
| `NEXT_PUBLIC_CLERK_SIGN_IN_FALLBACK_REDIRECT_URL` / `_SIGN_UP_…` | `/` / `/onboarding` | same |
| `NEXT_PUBLIC_APP_VERSION` | release tag (shown in the sidebar) | commit sha |

Leave `NEXT_PUBLIC_DEV_BEARER_TOKEN` unset everywhere. The web-push VAPID
public key comes from the API at runtime — nothing to set here.

The API side must match:

- the Hetzner env's `cors_origins` (GitHub variable `CORS_ORIGINS`) lists the
  exact Vercel production domain (and the staging API lists the preview
  domain you test from) — never `*`;
- `clerk_issuer` (`CLERK_ISSUER`) is the same Clerk instance's Frontend API
  URL as the publishable key.

Before the first production deploy, check that a `pnpm build && pnpm start`
with the production Clerk keys hydrates: every `<script>` carries the CSP
nonce (`middleware.ts`) and the console has no CSP violations.

## Notes

- **Trust boundary.** The browser only talks to the platform REST/WS. Never
  embed Darwin runtime code or vault values here.
- **CSP.** `middleware.ts` sets a per-request nonce policy (strict-dynamic in
  production) that allowlists the API, Clerk, and the Browserbase + Browser
  Use Cloud `frame-src` domains (captcha takeover).
- **Idempotency.** `lib/api.ts` auto-adds `Idempotency-Key` to every mutating
  POST; `kill` is generated per-modal-open so retrying the button never produces
  a second order.
- **Polling.** Timeline polls page 0 every 3s and dedupes by `event_id`.
  WebSocket replaces polling in M3.
