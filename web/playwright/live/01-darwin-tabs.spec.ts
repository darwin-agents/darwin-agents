/**
 * Live (no-mock) sweep of every dashboard surface against the real stack.
 *
 * Pre-conditions: `scripts/dev-up.sh` has the platform on :8000 and the
 * dashboard on :3000 (override via E2E_LIVE_WEB_URL / E2E_LIVE_API_URL).
 * Run with `npm run test:e2e:live`.
 *
 * The suite finds (or spawns) a live Darwin via the real API, then visits
 * every tab asserting (a) no unexpected >=400 API response and (b) the
 * tab's key content rendered — i.e. the screens are wired to endpoints
 * that exist AND return the shape the components consume. This is the
 * drift detector the route-stubbed `dev-token` project cannot be.
 */
import { expect, test, type Page } from "@playwright/test";

const API = process.env.E2E_LIVE_API_URL ?? "http://localhost:8000";

// Failures we accept: endpoints whose backing service is legitimately
// unconfigured in a local stack. Keep this list SHORT — every entry is
// a feature the live gate no longer guards.
const ALLOWED_FAILURES: Array<{ path: RegExp; status: number }> = [
  // KYC requires a provider sandbox account (operator-supplied creds).
  { path: /^\/platform\/kyc\/status$/, status: 503 },
];

let darwinId: string;

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, init);
  if (!res.ok) throw new Error(`${init?.method ?? "GET"} ${path} → ${res.status}`);
  return (await res.json()) as T;
}

test.beforeAll(async () => {
  // Stack must be up — fail fast with a clear message instead of 20
  // per-tab timeouts.
  const ready = await fetch(`${API}/readyz`).then((r) => r.ok).catch(() => false);
  if (!ready) throw new Error(`platform API not ready at ${API} — run scripts/dev-up.sh first`);

  // Reuse a live Darwin when one exists; spawn otherwise.
  const { darwins } = await api<{ darwins: string[] }>("/darwins");
  for (const id of darwins) {
    const st = await api<{ alive: boolean }>(`/darwins/${id}`);
    if (st.alive) {
      darwinId = id;
      return;
    }
  }
  const spawned = await api<{ darwin_id: string }>("/darwins", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": `live-e2e-${Date.now()}`,
    },
    body: JSON.stringify({ opening_usdc: 25 }),
  });
  darwinId = spawned.darwin_id;
});

/** Sign in via the dev-token paste path and arm the API-failure recorder. */
async function arm(page: Page): Promise<string[]> {
  const failures: string[] = [];
  page.on("response", (r) => {
    const url = new URL(r.url());
    if (`${url.protocol}//${url.host}` !== API || r.status() < 400) return;
    const allowed = ALLOWED_FAILURES.some(
      (a) => a.path.test(url.pathname) && a.status === r.status(),
    );
    if (!allowed) failures.push(`${r.status()} ${r.request().method()} ${url.pathname}`);
  });
  await page.goto("/", { waitUntil: "load" });
  await page.evaluate(() => localStorage.setItem("darwin_dev_token", "live-e2e"));
  return failures;
}

async function visit(page: Page, path: string): Promise<void> {
  await page.goto(path, { waitUntil: "load" });
  // Let the initial query waterfall settle (react-query + WS handshake).
  await page.waitForTimeout(4000);
}

test("fleet lists the darwin and tabs render real data", async ({ page }) => {
  const failures = await arm(page);

  await visit(page, "/");
  await expect(page.getByText("Fleet")).toBeVisible();
  await expect(page.getByText(darwinId.slice(0, 8)).first()).toBeVisible();

  // Summary — header + treasury strip render from real status/ledger.
  await visit(page, `/darwins/${darwinId}/summary`);
  await expect(page.getByText(/Couldn(')?t load/i)).toHaveCount(0);

  // Anatomy — the canvas page must reach "N nodes", not the error card
  // and not an eternal skeleton.
  await visit(page, `/darwins/${darwinId}/anatomy`);
  await expect(page.getByText(/\d+ nodes/)).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/Couldn(')?t load the anatomy/)).toHaveCount(0);

  // Timeline — real audit events stream in (birth event at minimum).
  await visit(page, `/darwins/${darwinId}/timeline`);
  await expect(page.getByText(/event:birth|birth/).first()).toBeVisible({
    timeout: 20_000,
  });

  expect(failures, `unexpected API failures:\n${failures.join("\n")}`).toEqual([]);
});

test("money + control tabs are live", async ({ page }) => {
  const failures = await arm(page);

  await visit(page, `/darwins/${darwinId}/billing`);
  await expect(page.getByText("Treasury")).toBeVisible();
  await expect(page.getByText(/Failed to load/i)).toHaveCount(0);

  await visit(page, `/darwins/${darwinId}/settings`);
  await expect(page.getByText("Spend lock")).toBeVisible();
  await expect(page.getByText(/Failed to load/i)).toHaveCount(0);

  await visit(page, `/darwins/${darwinId}/vault`);
  await expect(page.getByText(/Failed to load|HTTP \d\d\d/i)).toHaveCount(0);

  await visit(page, `/darwins/${darwinId}/resources`);
  await expect(page.getByText("Provisioned resources")).toBeVisible();
  await expect(page.getByText(/Failed to load/i)).toHaveCount(0);

  await visit(page, `/darwins/${darwinId}/preapprovals`);
  await expect(page.getByText(/Failed to load/i)).toHaveCount(0);

  await visit(page, `/darwins/${darwinId}/proposals`);
  await expect(page.getByText(/Failed to load/i)).toHaveCount(0);

  await visit(page, `/darwins/${darwinId}/lineage`);
  await expect(page.getByText(/Failed to load/i)).toHaveCount(0);

  await visit(page, `/darwins/${darwinId}/operator`);
  await expect(page.getByText("Operator controls")).toBeVisible();
  await expect(page.getByText(/Failed to load/i)).toHaveCount(0);

  expect(failures, `unexpected API failures:\n${failures.join("\n")}`).toEqual([]);
});

test("global surfaces are live", async ({ page }) => {
  const failures = await arm(page);

  await visit(page, "/escalations");
  await expect(page.getByText("Escalations")).toBeVisible();

  await visit(page, "/operator");
  await expect(page.getByText("Operator console")).toBeVisible();

  await visit(page, "/freelancers");
  await expect(page.getByText("Freelancers")).toBeVisible();

  // Notifications — VAPID is configured in the local stack, so the page
  // must offer the enable flow (not the "platform wasn't started with
  // Web Push" banner).
  await visit(page, "/notifications");
  await expect(page.getByText(/wasn(')?t started with Web Push/)).toHaveCount(0);

  expect(failures, `unexpected API failures:\n${failures.join("\n")}`).toEqual([]);
});

test("live WS stream feeds the anatomy page", async ({ page }) => {
  await arm(page);
  await visit(page, `/darwins/${darwinId}/anatomy`);
  // The ConnectionPill flips to "live" only after the WS handshake +
  // first snapshot — k8s darwins exercise the pod-RPC stream path.
  await expect(page.getByText("live", { exact: true })).toBeVisible({
    timeout: 30_000,
  });
});
