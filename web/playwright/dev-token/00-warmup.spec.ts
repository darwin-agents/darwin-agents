import { test } from "@playwright/test";

/**
 * First-test warmup. `next dev` compiles each route on demand, and the very
 * first hit to `/` (and the JS bundle download / hydration) can run ~30-60s
 * on a cold server. Letting that happen during a real assertion drowns the
 * test in timeouts; doing it here once lets the rest of the suite get clean
 * sub-10s runs. No assertions — just touch the routes the suite uses.
 */
test("warmup", async ({ page }) => {
  test.setTimeout(180_000);
  // `load` (not networkidle — Next dev's HMR WS keeps the network busy).
  await page.goto("/", { waitUntil: "load" });
  await page.goto("/darwins/x/anatomy", { waitUntil: "load" });
  await page.goto("/darwins/x/timeline", { waitUntil: "load" });
  await page.goto("/darwins/x/data", { waitUntil: "load" });
});
