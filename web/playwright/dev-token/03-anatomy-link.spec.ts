import { expect, mockBackend, signIn, test } from "../fixtures";

const DARWIN_ID = "alpha-001";

test("anatomy canvas links to the timeline filtered by neuron", async ({ page }) => {
  await mockBackend(page, {
    "/darwins": () => ({ darwins: [DARWIN_ID] }),
    [`/darwins/${DARWIN_ID}`]: () => ({
      darwin_id: DARWIN_ID,
      lifecycle: "alive",
    }),
    [`/darwins/${DARWIN_ID}/anatomy`]: () => ({
      darwin_id: DARWIN_ID,
      orchestrator: { id: "orch", label: "Orchestrator", state: "alive", tick: 1 },
      neurons: [
        {
          id: "content.publisher",
          label: "publisher",
          category: "content",
          status: "active",
          last_dispatch_at: null,
          last_error_at: null,
          dispatch_count_recent: 1,
          version: null,
        },
      ],
      edges: [
        { from: "orch", to: "content.publisher", kind: "dispatch", active: true, weight: 1 },
      ],
      heatmap: { "content.publisher": 1 },
      dna: { generation: 0, layers: [] },
    }),
    [`/darwins/${DARWIN_ID}/events`]: () => ({
      darwin_id: DARWIN_ID,
      total: 0,
      offset: 0,
      limit: 100,
      events: [],
    }),
    [`/darwins/${DARWIN_ID}/billing`]: () => ({ darwin_id: DARWIN_ID }),
  });
  await signIn(page);

  await page.goto(`/darwins/${DARWIN_ID}/anatomy`);

  // Click the neuron node by its accessible label.
  await page
    .getByRole("button", { name: /Neuron publisher/ })
    .click();

  // Side panel "View timeline for content.publisher" link.
  await page
    .getByRole("link", { name: /View timeline for content\.publisher/ })
    .click();

  await expect(page).toHaveURL(
    /\/timeline\?neuron=content\.publisher$/,
  );
});
