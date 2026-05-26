/**
 * Bearer-token shim.
 *
 * - In dev, reads `darwin_dev_token` from localStorage (paste-the-token flow).
 *   Falls back to NEXT_PUBLIC_DEV_BEARER_TOKEN for headless / Playwright runs.
 * - In production, this module is a stub — wrap `useAuth().getToken()` from
 *   `@clerk/nextjs` once the Clerk SDK is installed (M0-B follow-up). The
 *   backend already accepts Clerk RS256 JWTs directly (see security/clerk.py).
 */

const STORAGE_KEY = "darwin_dev_token";

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
 * Token resolver used by `lib/api.ts`. Production builds will replace the body
 * with `await clerk.session?.getToken()`; the call signature stays identical.
 */
export async function getAuthToken(): Promise<string | null> {
  return getDevToken();
}
