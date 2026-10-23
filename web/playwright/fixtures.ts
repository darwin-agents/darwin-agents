import type { DarwinStatus } from "../lib/types";
/**
 * Shared fixtures for Darwin E2E tests (dev-token project).
 *
 * - `signIn(page)` — seeds the dev bearer token in localStorage so the
 *   dashboard renders without the paste-token form.
 * - `mockBackend(page, handlers)` — registers `page.route()` interceptors
 *   for `${API_BASE}/**` so tests can return fixed payloads without
 *   booting the Python platform.
 */
import { test as base, expect, type Page } from "@playwright/test";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** Must match the token the webServer was started with. */
const DEV_TOKEN = "e2e-dev-token";

export async function signIn(page: Page): Promise<void> {
  // First visit primes localStorage on the right origin; `load` (not
  // networkidle — Next's HMR WebSocket keeps "network" busy in dev).
  await page.goto("/", { waitUntil: "load" });
  await page.evaluate((token) => window.localStorage.setItem("darwin_dev_token", token), DEV_TOKEN);
  await page.goto("/", { waitUntil: "load" });
  // Wait for DevAuthProvider hydration to flip past the "Loading…" placeholder.
  await page.getByText("Loading…", { exact: true }).waitFor({ state: "hidden", timeout: 45_000 });
}

export type MockHandler = (req: { method: string; url: URL }) => Promise<unknown> | unknown;

export interface BackendHandlers {
  [pathOrPattern: string]: MockHandler;
}

/**
 * Intercept backend calls. Match keys against `url.pathname`:
 * - exact match wins first
 * - then longest prefix match (so `/darwins/abc/anatomy` beats `/darwins`)
 * - default = empty 200 (so tests never hang on an un-stubbed call)
 *
 * `/healthz` and `/readyz` are silently 200'd regardless of handlers.
 */
export async function mockBackend(page: Page, handlers: BackendHandlers): Promise<void> {
  await page.route(`${API_BASE}/**/*`, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const method = req.method();

    if (url.pathname === "/healthz" || url.pathname === "/readyz") {
      return route.fulfill({
        status: 200,
        body: JSON.stringify({ status: "ok" }),
      });
    }

    const keys = Object.keys(handlers).sort((a, b) => b.length - a.length);
    for (const key of keys) {
      if (url.pathname === key || url.pathname.startsWith(key + "/")) {
        const handler = handlers[key];
        if (!handler) continue;
        const body = await handler({ method, url });
        return route.fulfill({
          status: 200,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      }
    }
    return route.fulfill({ status: 200, body: "{}" });
  });
}

/** A complete `GET /darwins/{id}` body in the backend's real shape (the
 *  generated type), so mocks can't drift from what the API returns. */
export function mockStatus(darwin_id: string, overrides: Partial<DarwinStatus> = {}): DarwinStatus {
  return {
    darwin_id,
    generation: 0,
    parent_id: null,
    l0_version: "0.0.6",
    birth_ts: 1_700_000_000,
    spawned_at: 1_700_000_000,
    alive: true,
    paused: false,
    killed: false,
    quarantined: false,
    quarantine_reason: null,
    death_reason: null,
    balance_usdc: 10,
    tick: 1,
    days_elapsed: 0,
    open_escalations: 0,
    crashed: null,
    address: "0x" + "a".repeat(40),
    profit_usdc: 0,
    unsettled_usdc: 0,
    payout_address: null,
    ...overrides,
  } as DarwinStatus;
}

export const test = base.extend({});
export { expect };
