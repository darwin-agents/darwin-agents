import { expect, mockBackend, mockStatus, signIn, test } from "../fixtures";

const DARWIN_ID = "alpha-001";

test("Timeline filter bar round-trips through the URL", async ({ page }) => {
  await mockBackend(page, {
    "/darwins": () => ({ darwins: [DARWIN_ID] }),
    [`/darwins/${DARWIN_ID}`]: () => mockStatus(DARWIN_ID),
    [`/darwins/${DARWIN_ID}/events`]: ({ url }) => ({
      darwin_id: DARWIN_ID,
      total: 1,
      offset: 0,
      limit: 100,
      events: [
        {
          seq: 1,
          ts: 1700000000,
          // Mirror the filter back so the UI shows it's wired.
          kind: url.searchParams.get("kind") ?? "event:perceive",
          neuron_id: url.searchParams.get("neuron_id") ?? "orchestrator",
        },
      ],
    }),
    [`/darwins/${DARWIN_ID}/billing`]: () => ({ darwin_id: DARWIN_ID }),
  });
  await signIn(page);

  await page.goto(`/darwins/${DARWIN_ID}/timeline`);
  await expect(page.getByText("Kind prefix")).toBeVisible();

  await page.getByPlaceholder("event: / intent: / outcome").fill("event:payment");
  // Debounce is 350 ms — wait for the URL to update.
  await expect(page).toHaveURL(/[?&]kind=event%3Apayment/);

  await page.getByPlaceholder("content.publisher").fill("content.writer");
  await expect(page).toHaveURL(/[?&]neuron=content\.writer/);

  // Clear button removes both.
  await page.getByRole("button", { name: /Clear/ }).click();
  await expect(page).toHaveURL(new RegExp(`^.+/darwins/${DARWIN_ID}/timeline/?$`));
});
