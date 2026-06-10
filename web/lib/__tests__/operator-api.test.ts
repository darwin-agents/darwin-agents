/**
 * Targeted tests for the operator / proposals / KYC api.ts methods.
 *
 * Mocks globalThis.fetch and asserts the request shape (method, path, body,
 * Idempotency-Key on mutations) — the contract the Proposals, Operator
 * console, and Freelancers KYC surfaces bind against.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";

interface FetchCall {
  url: string;
  init: RequestInit;
}

function mockFetch(responder: () => Response | Promise<Response>) {
  const original = globalThis.fetch;
  const calls: FetchCall[] = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), init: init ?? {} });
    return responder();
  }) as unknown as typeof fetch;
  return { calls, restore: () => (globalThis.fetch = original) };
}

const okJson = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

const headersOf = (init: RequestInit): Record<string, string> =>
  (init.headers ?? {}) as Record<string, string>;

const bodyOf = (init: RequestInit) =>
  init.body ? JSON.parse(init.body as string) : undefined;

afterEach(() => {
  vi.restoreAllMocks();
});

describe("proposals api", () => {
  it("listProposals hits GET /darwins/{id}/proposals", async () => {
    const { calls, restore } = mockFetch(() => okJson({ pending: [], admitted: [] }));
    await api.listProposals("d-1");
    expect(calls[0]!.url).toMatch(/\/darwins\/d-1\/proposals$/);
    expect(calls[0]!.init.method ?? "GET").toBe("GET");
    restore();
  });

  it("admitProposal POSTs to the platform admit route with Idempotency-Key", async () => {
    const { calls, restore } = mockFetch(() => okJson({ admitted: "n1" }));
    await api.admitProposal("d-1", "neuron/one");
    expect(calls[0]!.url).toMatch(
      /\/platform\/darwins\/d-1\/proposals\/neuron%2Fone\/admit$/,
    );
    expect(calls[0]!.init.method).toBe("POST");
    expect(headersOf(calls[0]!.init)["Idempotency-Key"]).toBeTruthy();
    restore();
  });

  it("rejectProposal sends the reason in the body", async () => {
    const { calls, restore } = mockFetch(() => okJson({ rejected: "n1" }));
    await api.rejectProposal("d-1", "n1", "not good enough");
    expect(calls[0]!.url).toMatch(/\/proposals\/n1\/reject$/);
    expect(bodyOf(calls[0]!.init)).toEqual({ reason: "not good enough" });
    restore();
  });
});

describe("throttle api", () => {
  it("setThrottle POSTs tier + max_daily_calls", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ tier: "fast", max_daily_calls: 10, throttles: {} }),
    );
    await api.setThrottle("d-1", { tier: "fast", max_daily_calls: 10 });
    expect(calls[0]!.url).toMatch(/\/platform\/darwins\/d-1\/throttle$/);
    expect(calls[0]!.init.method).toBe("POST");
    expect(bodyOf(calls[0]!.init)).toEqual({ tier: "fast", max_daily_calls: 10 });
    restore();
  });

  it("deleteThrottle passes tier as a query param", async () => {
    const { calls, restore } = mockFetch(() => okJson({ throttles: {} }));
    await api.deleteThrottle("d-1", "coding");
    expect(calls[0]!.url).toMatch(/\/throttle\?tier=coding$/);
    expect(calls[0]!.init.method).toBe("DELETE");
    restore();
  });

  it("setResourceThrottle POSTs kind + max_daily_calls", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ darwin_id: "d-1", kind: "browser", max_daily_calls: 5, installed_at: 0 }),
    );
    await api.setResourceThrottle("d-1", { kind: "browser", max_daily_calls: 5 });
    expect(calls[0]!.url).toMatch(/\/resource_throttle$/);
    expect(bodyOf(calls[0]!.init)).toEqual({ kind: "browser", max_daily_calls: 5 });
    restore();
  });
});

describe("wallet policy + quarantine api", () => {
  it("setWalletPolicy POSTs the three caps", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ per_action_cap_usdc: 1, per_day_cap_usdc: null, per_lifetime_cap_usdc: null }),
    );
    await api.setWalletPolicy("d-1", {
      per_action_cap_usdc: 1,
      per_day_cap_usdc: null,
      per_lifetime_cap_usdc: null,
    });
    expect(calls[0]!.url).toMatch(/\/platform\/wallets\/d-1\/policy$/);
    expect(bodyOf(calls[0]!.init)).toEqual({
      per_action_cap_usdc: 1,
      per_day_cap_usdc: null,
      per_lifetime_cap_usdc: null,
    });
    restore();
  });

  it("quarantine POSTs the reason", async () => {
    const { calls, restore } = mockFetch(() => okJson({ status: "quarantined" }));
    await api.quarantine("d-1", { reason: "suspicious" });
    expect(calls[0]!.url).toMatch(/\/platform\/darwins\/d-1\/quarantine$/);
    expect(bodyOf(calls[0]!.init)).toEqual({ reason: "suspicious" });
    restore();
  });

  it("clearQuarantine DELETEs the quarantine", async () => {
    const { calls, restore } = mockFetch(() => okJson({ status: "cleared" }));
    await api.clearQuarantine("d-1");
    expect(calls[0]!.url).toMatch(/\/quarantine$/);
    expect(calls[0]!.init.method).toBe("DELETE");
    restore();
  });
});

describe("fleet api", () => {
  it("fleetPause POSTs to the fleet pause route", async () => {
    const { calls, restore } = mockFetch(() => okJson({ paused: [] }));
    await api.fleetPause();
    expect(calls[0]!.url).toMatch(/\/platform\/fleet\/pause$/);
    expect(calls[0]!.init.method).toBe("POST");
    restore();
  });

  it("fleetSpendLock sends locked flag", async () => {
    const { calls, restore } = mockFetch(() => okJson({ spend_locked: [] }));
    await api.fleetSpendLock(true);
    expect(bodyOf(calls[0]!.init)).toEqual({ locked: true });
    restore();
  });

  it("fleetBlockCategory sends category", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ category: "ad_network", blocked_on: [] }),
    );
    await api.fleetBlockCategory("ad_network");
    expect(calls[0]!.url).toMatch(/\/fleet\/block_category$/);
    expect(bodyOf(calls[0]!.init)).toEqual({ category: "ad_network" });
    restore();
  });
});

describe("staging + signing api", () => {
  it("stagingHistory passes the limit query", async () => {
    const { calls, restore } = mockFetch(() => okJson({ verdicts: [], total: 0 }));
    await api.stagingHistory(25);
    expect(calls[0]!.url).toMatch(/\/platform\/staging\/history\?limit=25$/);
    restore();
  });

  it("rotateSigningKey POSTs a drain window", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ retired_public_key_pem: "x", drain_until: 0 }),
    );
    await api.rotateSigningKey(60);
    expect(calls[0]!.url).toMatch(/\/platform\/signing\/rotate$/);
    expect(bodyOf(calls[0]!.init)).toEqual({ drain_window_seconds: 60 });
    restore();
  });
});

describe("kyc api", () => {
  it("kycStart POSTs the tier", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ inquiry_id: "i1", redirect_url: "https://x", status: "pending" }),
    );
    await api.kycStart({ tier: "enhanced" });
    expect(calls[0]!.url).toMatch(/\/platform\/kyc\/start$/);
    expect(bodyOf(calls[0]!.init)).toEqual({ tier: "enhanced" });
    expect(headersOf(calls[0]!.init)["Idempotency-Key"]).toBeTruthy();
    restore();
  });

  it("kycStatus reads the owner verdict", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ subject: "me", status: "approved" }),
    );
    await api.kycStatus();
    expect(calls[0]!.url).toMatch(/\/platform\/kyc\/status$/);
    expect(calls[0]!.init.method ?? "GET").toBe("GET");
    restore();
  });
});
