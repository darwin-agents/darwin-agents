/**
 * Single source of truth for "is Clerk wired?".
 *
 * Decided at build time from `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` so both the
 * server (middleware, layouts) and the client (api/ws) agree. When unset, the
 * dashboard falls back to the dev token-paste path — needed for headless /
 * Playwright runs against a platform booted with `--auth-tokens`.
 */
export const CLERK_ENABLED: boolean = Boolean(
  process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY,
);
