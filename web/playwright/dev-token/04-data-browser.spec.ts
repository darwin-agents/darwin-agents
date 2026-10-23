import { expect, mockBackend, mockStatus, signIn, test } from "../fixtures";

const DARWIN_ID = "alpha-001";

test("data browser tabs switch via URL", async ({ page }) => {
  await mockBackend(page, {
    "/darwins": () => ({ darwins: [DARWIN_ID] }),
    [`/darwins/${DARWIN_ID}`]: () => mockStatus(DARWIN_ID),
    [`/darwins/${DARWIN_ID}/lineage/tree`]: () => ({
      darwin_id: DARWIN_ID,
      root: { darwin_id: DARWIN_ID, generation: 1 },
      ancestors: [],
      descendants: [],
    }),
    [`/darwins/${DARWIN_ID}/ledger`]: () => ({
      darwin_id: DARWIN_ID,
      total: 1,
      offset: 0,
      limit: 100,
      entries: [
        {
          seq: 1,
          ts: "2026-05-30T00:00:00Z",
          type: "credit",
          direction: "in",
          amount_usdc: 10,
          counterparty: "operator",
          category: "fund",
          memo: "seed",
        },
      ],
    }),
    [`/darwins/${DARWIN_ID}/events`]: () => ({
      darwin_id: DARWIN_ID,
      total: 1,
      offset: 0,
      limit: 100,
      events: [{ seq: 1, ts: 1700000000, kind: "event:perceive", hash: "0xabcd1234" }],
    }),
  });
  await signIn(page);

  await page.goto(`/darwins/${DARWIN_ID}/data`);

  // Default tab: lifecycle. Expect a row labeled "darwin_id".
  await expect(page.getByText("darwin_id").first()).toBeVisible();

  // Switch to ledger.
  await page.getByRole("button", { name: "ledger" }).click();
  await expect(page).toHaveURL(/[?&]tab=ledger/);
  await expect(page.getByText("credit").first()).toBeVisible();

  // Switch to audit.
  await page.getByRole("button", { name: "audit" }).click();
  await expect(page).toHaveURL(/[?&]tab=audit/);
  await expect(page.getByText("event:perceive").first()).toBeVisible();
});
