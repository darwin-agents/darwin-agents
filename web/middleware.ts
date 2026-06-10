/**
 * Edge middleware: Clerk auth gate + per-request Content-Security-Policy.
 *
 * CSP lives here (not in `next.config.ts` headers) so every HTML response
 * carries a fresh nonce. Next.js reads the nonce from the request's CSP
 * header and stamps it onto each framework `<script>`, which lets the App
 * Router's inline bootstrap/hydration scripts execute under a strict
 * `script-src` (no 'unsafe-inline') via 'strict-dynamic'. Without this the
 * production build's inline scripts are CSP-blocked and the app never
 * hydrates.
 *
 * Dev keeps 'unsafe-inline'/'unsafe-eval' (React Fast Refresh) and skips
 * the nonce — a nonce would make browsers ignore 'unsafe-inline' and break
 * Refresh.
 *
 * Auth, by build-time env:
 * - Clerk enabled  → `clerkMiddleware()` redirects unauthenticated requests
 *   to `/sign-in`. We use the explicit `redirectToSignIn()` over
 *   `auth.protect()` so the user lands on a sign-in page rather than a 404.
 * - Clerk disabled → pass-through so the dev-token paste flow keeps working
 *   (used by Playwright + the legacy `--auth-tokens` boot).
 */
import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";
import type { NextFetchEvent, NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { CLERK_ENABLED } from "@/lib/auth-mode";

const isProd = process.env.NODE_ENV === "production";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const apiOrigin = (() => {
  try {
    return new URL(API_BASE).origin;
  } catch {
    return "http://localhost:8000";
  }
})();
const wsOrigin = apiOrigin.replace(/^http/, "ws");

// Clerk's Frontend API origin is encoded in the publishable key
// (`pk_(test|live)_<base64(host$)>`). When Clerk is enabled we must allow its
// script/connect/image domains in the CSP or the widget never loads in prod.
function clerkFapiOrigin(): string | null {
  const pk = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;
  if (!pk) return null;
  const b64 = pk.replace(/^pk_(test|live)_/, "");
  try {
    const host = atob(b64).replace(/\$+$/, "");
    return host ? `https://${host}` : null;
  } catch {
    return null;
  }
}

const clerkFapi = clerkFapiOrigin();
// Clerk static assets + telemetry + dev-instance wildcard. The per-instance
// FAPI origin (clerkFapi) is added on top so production custom domains work.
const CLERK_SCRIPT = ["https://*.clerk.accounts.dev", "https://*.clerk.com"];
const CLERK_CONNECT = [
  "https://*.clerk.accounts.dev",
  "https://*.clerk.com",
  "https://clerk-telemetry.com",
];
const CLERK_IMG = ["https://img.clerk.com"];
// Clerk's bot-protection challenge renders in a Cloudflare Turnstile iframe.
const CLERK_FRAME = ["https://challenges.cloudflare.com"];

function cspFor(nonce: string): string {
  const clerkScript = CLERK_ENABLED
    ? ` ${CLERK_SCRIPT.join(" ")}${clerkFapi ? ` ${clerkFapi}` : ""}`
    : "";
  const scriptSrc = isProd
    ? `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${clerkScript}`
    : `script-src 'self' 'unsafe-eval' 'unsafe-inline'${clerkScript}`;

  const connect = [`'self'`, apiOrigin, wsOrigin];
  const frame = ["'self'", "https://*.browserbase.com", "https://*.browser-use.com"];
  const img = ["'self'", "data:", "blob:"];
  if (CLERK_ENABLED) {
    connect.push(...CLERK_CONNECT, ...(clerkFapi ? [clerkFapi] : []));
    frame.push(...CLERK_FRAME);
    img.push(...CLERK_IMG);
  }

  return [
    "default-src 'self'",
    `connect-src ${connect.join(" ")}`,
    `img-src ${img.join(" ")}`,
    "font-src 'self' data:",
    "style-src 'self' 'unsafe-inline'",
    scriptSrc,
    // Clerk runs its crypto in a web worker (blob:); needed for sign-in.
    "worker-src 'self' blob:",
    // Browserbase + Browser Use Cloud live-takeover iframes (M2 captcha flow)
    // plus Clerk's Turnstile challenge.
    `frame-src ${frame.join(" ")}`,
    "frame-ancestors 'none'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; ");
}

const isPublic = createRouteMatcher(["/sign-in(.*)", "/sign-up(.*)"]);

const clerkHandler = clerkMiddleware(async (auth, req) => {
  if (isPublic(req)) return;
  const { userId, redirectToSignIn } = await auth();
  if (!userId) return redirectToSignIn();
});

export default async function middleware(
  req: NextRequest,
  event: NextFetchEvent,
) {
  // btoa + crypto.randomUUID are both available in the Edge runtime.
  const nonce = isProd ? btoa(crypto.randomUUID()) : "";
  const csp = cspFor(nonce);

  // Pass the nonce to the renderer via the *request* CSP header so Next
  // stamps it onto its <script> tags. Clerk's own script/connect/img domains
  // are folded into the policy by `cspFor` when CLERK_ENABLED (see the
  // CLERK_* allowlists above); Clerk's script loads via 'strict-dynamic'
  // from Next's nonced bootstrap.
  const requestHeaders = new Headers(req.headers);
  if (isProd) requestHeaders.set("content-security-policy", csp);

  const res = CLERK_ENABLED
    ? (((await clerkHandler(req, event)) as NextResponse | undefined) ??
      NextResponse.next({ request: { headers: requestHeaders } }))
    : NextResponse.next({ request: { headers: requestHeaders } });

  res.headers.set("Content-Security-Policy", csp);
  return res;
}

export const config = {
  matcher: [
    // Skip Next.js internals + static files.
    "/((?!_next/|.*\\..*).*)",
    // Always run on API routes (none today, but keeps the gate future-proof).
    "/(api|trpc)(.*)",
  ],
};
