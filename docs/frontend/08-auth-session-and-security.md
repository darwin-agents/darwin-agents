# Auth, session, and security

## Phase 0 — development auth

Platform ships an in-memory bearer token store (`darwin_platform/security/auth.py`). For local dashboard dev:

1. Obtain token via platform CLI or seed script (documented in platform README when added).
2. Store in `localStorage` key `darwin_dev_token` or `.env.local` for automated E2E only.
3. `lib/auth.ts` attaches `Authorization: Bearer` on every `api.ts` call.

No Clerk in Phase 0. Do not commit tokens.

## Production auth — Clerk (backend already wired)

Clerk is the chosen and implemented auth vendor on the platform side (`src/darwin_platform/security/clerk.py`):

- `ClerkVerifier` validates Clerk RS256 JWTs against the issuer's JWKS.
- `ClerkTokenStore` is a drop-in for the `TokenStore` Protocol that `get_auth` already uses — claims map to scopes, and per-user Darwin ownership is keyed by Clerk user id.
- CLI wiring: `--clerk-issuer` / `--clerk-jwks-url`.

```
User → Clerk sign-in → Next.js session
     → useAuth().getToken() returns Clerk RS256 JWT
     → Send as Authorization: Bearer to FastAPI (no exchange step needed)
     → ClerkTokenStore maps the JWT to AuthContext + owned Darwins
```

Frontend requirements:

- One Clerk user maps to one platform `owner_id` (Clerk's `sub` claim).
- Short token TTL (Clerk default ~60 min) refreshed via `getToken()`; the FE re-fetches before every request rather than caching long.
- The platform enforces Darwin ownership on every route — hiding links in the FE is not security.

WorkOS is **not** wired and is no longer a planned alternative; if multi-IdP support is ever needed, add a second `TokenStore` implementation matching the same Protocol.

## Token storage in the browser

| Approach | Milestone | Notes |
|----------|-----------|-------|
| Memory only | M0 dev shim | Simplest; lost on refresh — acceptable for dev token paste |
| Clerk session + `getToken()` on every request | M0-B onward | The default for the production path; Clerk SDK handles refresh in its own session |
| httpOnly cookie via Next.js BFF route handler | Optional, M3+ | Only if we ever want to hide the bearer from the browser; backend doesn't require it |

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
