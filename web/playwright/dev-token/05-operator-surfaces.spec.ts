import { expect, mockBackend, mockStatus, signIn, test } from "../fixtures";

const ID = "alpha-001";

const darwinStub = mockStatus(ID);

test("proposals page renders pending + admitted, with operator actions when operator", async ({
  page,
}) => {
  await mockBackend(page, {
    "/platform/capabilities": () => ({
      tenant_namespaces: false,
      soma_forensics: false,
      k8s: false,
      organism_browser: false,
      forensics_viewer: false,
      is_operator: true,
    }),
    [`/darwins/${ID}`]: () => darwinStub,
    [`/darwins/${ID}/proposals`]: () => ({
      pending: [{ name: "neuron/payments-retry", tags: ["payments"], version: "2" }],
      admitted: [{ name: "neuron/base", tags: [], version: "1" }],
    }),
  });
  await signIn(page);

  await page.goto(`/darwins/${ID}/proposals`, { waitUntil: "load" });

  await expect(page.getByRole("heading", { name: "Proposals" })).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByText("neuron/payments-retry")).toBeVisible();
  await expect(page.getByText("neuron/base")).toBeVisible();
  // Operator token → admit/reject controls are present.
  await expect(page.getByRole("button", { name: "Admit" })).toBeVisible();
});

test("operator console is gated: owner token sees the operator-only notice", async ({ page }) => {
  await mockBackend(page, {
    "/platform/capabilities": () => ({
      tenant_namespaces: false,
      soma_forensics: false,
      k8s: false,
      organism_browser: false,
      forensics_viewer: false,
      is_operator: false, // owner, not operator
    }),
    [`/darwins/${ID}`]: () => darwinStub,
  });
  await signIn(page);

  await page.goto(`/darwins/${ID}/operator`, { waitUntil: "load" });

  // Body guard renders the operator-only notice instead of the console.
  await expect(page.getByText(/operator-only/i)).toBeVisible({ timeout: 15_000 });
});
