/**
 * Bearer-token resolver.
 *
 * Two paths, chosen at build time by `lib/auth-mode.ts`:
 * - Clerk enabled (`NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` set) → read the JWT
 *   from `window.Clerk.session.getToken()`. Backend verifies it against
 *   Clerk's JWKS (see `security/clerk.py`).
 * - Clerk disabled → fall back to `darwin_dev_token` in localStorage
 *   (paste-the-token flow), or `NEXT_PUBLIC_DEV_BEARER_TOKEN` for headless /
 *   Playwright runs against `--auth-tokens`.
 *
 * Same surface as before, so `lib/api.ts` and `lib/ws.ts` stay React-free.
 */
import { CLERK_ENABLED } from "./auth-mode";

const STORAGE_KEY = "darwin_dev_token";

type ClerkLike = {
  loaded?: boolean;
  session?: { getToken: () => Promise<string | null> } | null;
};

function getClerkInstance(): ClerkLike | null {
  if (typeof window === "undefined") return null;
  const clerk = (window as unknown as { Clerk?: ClerkLike }).Clerk;
  return clerk ?? null;
}

export function getDevToken(): string | null {
  if (typeof window === "undefined") {
    return process.env.NEXT_PUBLIC_DEV_BEARER_TOKEN || null;
  }
  try {
    const fromLs = window.localStorage.getItem(STORAGE_KEY);
    if (fromLs && fromLs.trim()) return fromLs.trim();
  } catch {
    /* localStorage disabled — fall through */
  }
  return process.env.NEXT_PUBLIC_DEV_BEARER_TOKEN || null;
}

export function setDevToken(token: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, token);
  } catch {
    /* localStorage disabled */
  }
}

export function clearDevToken(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* localStorage disabled */
  }
}

/**
 * Token resolver used by `lib/api.ts` and `lib/ws.ts`.
 *
 * Clerk path: waits up to ~2s for `window.Clerk` to finish loading on the
 * first call after page boot, then reads the active session JWT. If Clerk is
 * loaded but there's no session (signed out), returns null — fetches will go
 * out anonymous and surface as ApiError 401, which the layout already gates.
 */
export async function getAuthToken(): Promise<string | null> {
  if (CLERK_ENABLED) {
    const clerk = await waitForClerk();
    if (clerk?.session) {
      try {
        return await clerk.session.getToken();
      } catch {
        return null;
      }
    }
    return null;
  }
  return getDevToken();
}

async function waitForClerk(timeoutMs = 2000): Promise<ClerkLike | null> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const clerk = getClerkInstance();
    if (clerk?.loaded) return clerk;
    await new Promise((r) => setTimeout(r, 25));
  }
  return getClerkInstance();
}
