import { expect, mockBackend, mockStatus, signIn, test } from "../fixtures";

const ID = "alpha-001";

for (const state of [{ paused: true }, { crashed: "RuntimeError: boom" }]) {
  test(`a ${Object.keys(state)[0]} Darwin offers Resume and shows where money goes`, async ({
    page,
  }) => {
    await mockBackend(page, {
      "/darwins": () => ({ darwins: [ID] }),
      [`/darwins/${ID}`]: () =>
        mockStatus(ID, {
          ...state,
          address: "0x" + "a".repeat(40),
          payout_address: "0x52908400098527886E0F7030069857D2E4169EE7",
        }),
      [`/store/${ID}`]: () => ({
        darwin_id: ID,
        service: "research report",
        price_usdc: 0.5,
        payment: { accepts: [] },
      }),
    });
    await signIn(page);
    await page.goto(`/darwins/${ID}`);

    // Resume was missing entirely: the UI read an invented `lifecycle` field.
    await expect(page.getByRole("button", { name: "Resume" })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("0x" + "a".repeat(40))).toBeVisible();
    await expect(page.getByText("0x52908400098527886E0F7030069857D2E4169EE7")).toBeVisible();
    await expect(page.getByText(/Storefront · /)).toBeVisible();
  });
}
