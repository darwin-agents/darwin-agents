import { expect, mockBackend, mockStatus, signIn, test } from "../fixtures";

test("fleet page renders signed-in Darwin list", async ({ page }) => {
  await mockBackend(page, {
    "/darwins": () => ({ darwins: ["alpha-001", "beta-002"] }),
    "/darwins/alpha-001": () => mockStatus("alpha-001", { balance_usdc: 12.34 }),
    "/darwins/beta-002": () => mockStatus("beta-002", { balance_usdc: 5.6 }),
  });
  await signIn(page);

  await expect(page).toHaveURL(/\/$/);
  // The Fleet page has an <h1>Fleet</h1> — proves we made it past
  // <DevAuthProvider>'s gate (which renders a paste-token form instead).
  await expect(page.getByRole("heading", { name: "Fleet" })).toBeVisible({
    timeout: 15_000,
  });
  // And the mocked /darwins payload reaches the FleetRow children.
  await expect(page.getByText("alpha-001")).toBeVisible();
});
