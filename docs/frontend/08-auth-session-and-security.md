# Auth, session, and security

## Phase 0 — development auth

Platform ships an in-memory bearer token store (`darwin_platform/security/auth.py`). For local dashboard dev:

1. Obtain token via platform CLI or seed script (documented in platform README when added).
2. Store in `localStorage` key `darwin_dev_token` or `.env.local` for automated E2E only.
3. `lib/auth.ts` attaches `Authorization: Bearer` on every `api.ts` call.

No Clerk in Phase 0. Do not commit tokens.

## Phase 2 — production auth

```
User → Clerk/WorkOS sign-in → Next.js session
     → Server action or API route exchanges identity for platform JWT
     → Platform JWT scoped: owner, owned_darwins=[uuid,...]
```

Requirements:

- Multi-tenant: one Clerk user maps to one platform `owner_id`.
- JWT short TTL (15–60 min) with refresh via Clerk session refresh.
- Platform continues to enforce Darwin ownership on every route — frontend hiding links is not security.

WorkOS vs Clerk: decide at Phase 2 kickoff per [`../engineering/11-tech-stack-at-a-glance.md`](../engineering/11-tech-stack-at-a-glance.md); integration shape is identical for the dashboard.

## Token storage in the browser

| Approach | Phase | Notes |
|----------|-------|-------|
| Memory only | 0 | Simplest; lost on refresh — acceptable for dev |
| httpOnly cookie via Next.js BFF | 2+ | Preferred if we add route handlers to hold platform JWT |
| Clerk session + fetch token on demand | 2 | `getToken()` pattern |

Never store platform operator tokens in the owner app.

## Content Security Policy

Next.js headers (production):

- `default-src 'self'`
- `connect-src` includes API origin and WebSocket origin
- `frame-src` for Browserbase live takeover embed (escalation flow) — allowlist Browserbase domains only
- No `unsafe-inline` scripts without nonce (Next 15 defaults)

## Sensitive data in the UI

| Data | Rule |
|------|------|
| Vault secret values | Never render full secret; show fingerprint + last four |
| LLM prompts | Hidden by default; expand shows truncated hash reference |
| KYC documents | Presigned URL in iframe with short TTL; no caching |
| Wallet private keys | Never in frontend — only balances and tx hashes |

## Kill switch UX security

- Terminate requires typing Darwin id or checkbox "I understand this is permanent".
- Optional keyboard shortcut (Settings) — disabled by default; requires confirmation enable.
- Double-submit protection on kill POST.

## CSRF

Bearer-header API calls from SPA are not CSRF-vulnerable to the Python API the same way cookie auth is. If we move to cookie-based platform sessions, require CSRF tokens on mutating routes.

## E2E and CI

Playwright uses a dedicated test owner token injected via `PLAYWRIGHT_PLATFORM_TOKEN` in CI secrets — rotate independently of prod Clerk.

## Audit

All owner mutations initiated from UI should send optional header `X-Darwin-Client: web/1.0.0` for support correlation (platform may log it; not required for MVP).
