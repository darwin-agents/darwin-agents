import { test, expect } from "@playwright/test";

/**
 * The auth-gate spec only runs against the `clerk-gate` project, which is
 * spun up only when `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` is set in the env.
 * Verifies the middleware redirects unauthenticated requests to /sign-in.
 *
 * Does NOT exercise a real sign-in — that needs a provisioned Clerk test
 * user which lives outside the harness.
 */
test("middleware redirects unauthenticated requests to /sign-in", async ({ page }) => {
  const res = await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page).toHaveURL(/\/sign-in/);
  // Ensure we actually came through the middleware (not a JS-only redirect).
  expect(res?.request().redirectedFrom()).toBeTruthy();
});

test("sign-in page renders the Clerk SignIn component", async ({ page }) => {
  await page.goto("/sign-in");
  // Clerk renders a form into the page; we don't pin to a specific selector
  // since the SDK markup can change. Just confirm the page mounted.
  await expect(page.locator("body")).toContainText(/sign in|continue/i, {
    timeout: 15_000,
  });
});
