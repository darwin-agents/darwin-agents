/**
 * Playwright E2E config for the Darwin dashboard.
 *
 * Two projects:
 * - `dev-token`: webServer boots with `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=""`
 *   so the dev-token paste path is active. Covers fleet, timeline, anatomy
 *   link, data browser, spawn. Backend is stubbed per-test via
 *   `page.route()` so no `darwin-platform serve` boot is needed.
 * - `clerk-gate`: a second webServer with Clerk enabled. Single spec
 *   asserts the auth middleware redirects unauthenticated requests to
 *   `/sign-in`. Skipped automatically when `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`
 *   is missing from the environment.
 */
import { defineConfig, devices } from "@playwright/test";

const DEV_PORT = Number(process.env.E2E_DEV_PORT ?? 3100);
const CLERK_PORT = Number(process.env.E2E_CLERK_PORT ?? 3101);
const DEV_BASE = `http://localhost:${DEV_PORT}`;
const CLERK_BASE = `http://localhost:${CLERK_PORT}`;

const HAS_CLERK = Boolean(process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY);

const DEV_TOKEN = "e2e-dev-token";

export default defineConfig({
  testDir: "./playwright",
  fullyParallel: false,
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  // Default 30s isn't enough — `next dev` compiles routes on demand and the
  // first hit (route compile + JS bundle + React hydration) can take ~30s
  // alone, leaving no headroom for the actual assertions.
  timeout: 90_000,
  reporter: process.env.CI ? [["github"], ["list"]] : "list",
  use: {
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "dev-token",
      testMatch: /dev-token\/.*\.spec\.ts$/,
      use: {
        ...devices["Desktop Chrome"],
        baseURL: DEV_BASE,
        // Mirror the env the webServer below sees so test code can read it.
        extraHTTPHeaders: {},
      },
    },
    {
      name: "clerk-gate",
      testMatch: /clerk\/.*\.spec\.ts$/,
      use: {
        ...devices["Desktop Chrome"],
        baseURL: CLERK_BASE,
      },
    },
  ],
  // `next dev` is used so the env override below takes effect (next build's
  // env inlining + .env merge is finicky in CI). The trade-off is per-route
  // first-compile cost; `retries: 2` on CI absorbs the rare first-hit flake.
  webServer: [
    {
      command:
        `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY= ` +
        `NEXT_PUBLIC_DEV_BEARER_TOKEN=${DEV_TOKEN} ` +
        `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 ` +
        `pnpm exec next dev -p ${DEV_PORT}`,
      url: DEV_BASE,
      timeout: 60_000,
      reuseExistingServer: !process.env.CI,
    },
    ...(HAS_CLERK
      ? [
          {
            command: `pnpm exec next dev -p ${CLERK_PORT}`,
            url: CLERK_BASE,
            timeout: 60_000,
            reuseExistingServer: !process.env.CI,
            env: {
              NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY:
                process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY!,
              NEXT_PUBLIC_CLERK_SIGN_IN_URL: "/sign-in",
              NEXT_PUBLIC_CLERK_SIGN_UP_URL: "/sign-up",
              NEXT_PUBLIC_CLERK_SIGN_IN_FALLBACK_REDIRECT_URL: "/",
              NEXT_PUBLIC_CLERK_SIGN_UP_FALLBACK_REDIRECT_URL: "/",
              CLERK_SECRET_KEY: process.env.CLERK_SECRET_KEY ?? "",
              NEXT_PUBLIC_API_BASE_URL: "http://localhost:8000",
            },
          },
        ]
      : []),
  ],
});

export { DEV_TOKEN };
