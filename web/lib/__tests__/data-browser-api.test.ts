/**
 * Targeted tests for the #24-frontend api.ts methods: the ledger accounting
 * summary (docs 05, 17), the operator organism data browser, and post-mortem
 * forensics (doc 05). Mocks globalThis.fetch and asserts the request shape —
 * these are GET reads bound by the Tax-summary, Data > Organism, and Forensics
 * pages; if a path drifts, those pages break.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";

interface FetchCall {
  url: string;
  init: RequestInit;
}

function mockFetch(
  responder: () => Response | Promise<Response>,
): { calls: FetchCall[]; restore: () => void } {
  const original = globalThis.fetch;
  const calls: FetchCall[] = [];
  globalThis.fetch = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init: init ?? {} });
      return responder();
    },
  ) as unknown as typeof fetch;
  return { calls, restore: () => (globalThis.fetch = original) };
}

const okJson = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

afterEach(() => {
  vi.restoreAllMocks();
});

/* ----------------------------- Ledger summary -------------------------- */

describe("ledger summary api", () => {
  it("ledgerSummary omits year for the lifetime view", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ currency: "USDC", net_usdc: 0 }),
    );
    await api.ledgerSummary("d-1");
    const url = new URL(calls[0]!.url);
    expect(url.pathname).toMatch(/\/darwins\/d-1\/ledger\/summary$/);
    expect(url.searchParams.has("year")).toBe(false);
    expect(calls[0]!.init.method ?? "GET").toBe("GET");
    restore();
  });

  it("ledgerSummary passes the tax year as a query param", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ currency: "USDC", period: { year: 2025 } }),
    );
    await api.ledgerSummary("d-1", 2025);
    const url = new URL(calls[0]!.url);
    expect(url.searchParams.get("year")).toBe("2025");
    restore();
  });
});

/* ----------------------------- Capabilities ---------------------------- */

describe("capabilities api", () => {
  it("capabilities hits GET /platform/capabilities", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ tenant_namespaces: false, organism_browser: false }),
    );
    await api.capabilities();
    expect(calls[0]!.url).toMatch(/\/platform\/capabilities$/);
    expect(calls[0]!.init.method ?? "GET").toBe("GET");
    restore();
  });
});

/* --------------------------- Organism data browser --------------------- */

describe("organism data browser api", () => {
  it("organismTables hits the operator-scoped tables route", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ darwin_id: "d-1", tables: ["orders"] }),
    );
    await api.organismTables("d-1");
    expect(calls[0]!.url).toMatch(
      /\/platform\/darwins\/d-1\/organism\/tables$/,
    );
    restore();
  });

  it("organismRows encodes the table and binds limit/offset", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ darwin_id: "d-1", table: "orders", columns: [], rows: [] }),
    );
    await api.organismRows("d-1", "orders", { limit: 25, offset: 50 });
    const url = new URL(calls[0]!.url);
    expect(url.pathname).toMatch(
      /\/platform\/darwins\/d-1\/organism\/tables\/orders\/rows$/,
    );
    expect(url.searchParams.get("limit")).toBe("25");
    expect(url.searchParams.get("offset")).toBe("50");
    restore();
  });

  it("organismRows defaults to a 50-row page from offset 0", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ darwin_id: "d-1", table: "t", columns: [], rows: [] }),
    );
    await api.organismRows("d-1", "t");
    const url = new URL(calls[0]!.url);
    expect(url.searchParams.get("limit")).toBe("50");
    expect(url.searchParams.get("offset")).toBe("0");
    restore();
  });
});

/* ------------------------------- Forensics ----------------------------- */

describe("forensics api", () => {
  it("forensics hits the operator-scoped index route", async () => {
    const { calls, restore } = mockFetch(() => okJson({ snapshots: [] }));
    await api.forensics("d-1");
    expect(calls[0]!.url).toMatch(/\/platform\/darwins\/d-1\/forensics$/);
    restore();
  });

  it("forensicSnapshot encodes the snapshot id in the path", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ snapshot_id: "death-123" }),
    );
    await api.forensicSnapshot("d-1", "death-123");
    expect(calls[0]!.url).toMatch(
      /\/platform\/darwins\/d-1\/forensics\/death-123$/,
    );
    restore();
  });
});
