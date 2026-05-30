/**
 * Clerk auth middleware.
 *
 * Two-mode by build-time env:
 * - Clerk enabled  → `clerkMiddleware()` redirects unauthenticated requests
 *   to `/sign-in`. We use the explicit `redirectToSignIn()` over
 *   `auth.protect()` so the user lands on a sign-in page rather than a
 *   404 (Clerk 7's protect default is "hide existence").
 * - Clerk disabled → no-op pass-through so the dev-token paste flow keeps
 *   working (used by Playwright + the legacy `--auth-tokens` boot).
 */
import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";
import type { NextFetchEvent, NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { CLERK_ENABLED } from "@/lib/auth-mode";

const isPublic = createRouteMatcher([
  "/sign-in(.*)",
  "/sign-up(.*)",
]);

const clerkHandler = clerkMiddleware(async (auth, req) => {
  if (isPublic(req)) return;
  const { userId, redirectToSignIn } = await auth();
  if (!userId) return redirectToSignIn();
});

export default function middleware(req: NextRequest, event: NextFetchEvent) {
  if (!CLERK_ENABLED) return NextResponse.next();
  return clerkHandler(req, event);
}

export const config = {
  matcher: [
    // Skip Next.js internals + static files.
    "/((?!_next/|.*\\..*).*)",
    // Always run on API routes (none today, but keeps the gate future-proof).
    "/(api|trpc)(.*)",
  ],
};
