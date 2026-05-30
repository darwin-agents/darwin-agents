/**
 * Clerk auth middleware.
 *
 * Two-mode by build-time env:
 * - Clerk enabled  → `clerkMiddleware()` protects every dashboard route and
 *   redirects unauthenticated requests to `/sign-in`.
 * - Clerk disabled → no-op pass-through so the dev-token paste flow keeps
 *   working (used by Playwright + the legacy `--auth-tokens` boot).
 *
 * Public routes (always allowed): /sign-in/*, /sign-up/*, Next.js assets.
 */
import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";
import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { CLERK_ENABLED } from "@/lib/auth-mode";

const isPublic = createRouteMatcher([
  "/sign-in(.*)",
  "/sign-up(.*)",
]);

const clerkHandler = clerkMiddleware(async (auth, req) => {
  if (isPublic(req)) return;
  await auth.protect();
});

export default function middleware(req: NextRequest) {
  if (!CLERK_ENABLED) return NextResponse.next();
  return clerkHandler(req, {} as never);
}

export const config = {
  matcher: [
    // Skip Next.js internals + static files.
    "/((?!_next/|.*\\..*).*)",
    // Always run on API routes (none today, but keeps the gate future-proof).
    "/(api|trpc)(.*)",
  ],
};
