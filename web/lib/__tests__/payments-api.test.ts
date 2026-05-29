/**
 * Targeted tests for the payments-related api.ts methods (doc 17).
 *
 * Mocks globalThis.fetch and asserts the request shape (method, path,
 * body, Idempotency-Key on mutations). The API surface is what the
 * Pre-approvals / Freelancers / Payments pages bind against; if any
 * of these drift, those pages break.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";

interface FetchCall {
  url: string;
  init: RequestInit;
}

function mockFetch(
  responder: () => Response | Promise<Response>,
): { calls: FetchCall[]; first: () => FetchCall; restore: () => void } {
  const original = globalThis.fetch;
  const calls: FetchCall[] = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), init: init ?? {} });
    return responder();
  }) as unknown as typeof fetch;
  const first = () => {
    const c = calls[0];
    if (!c) throw new Error("expected at least one fetch call");
    return c;
  };
  return { calls, first, restore: () => (globalThis.fetch = original) };
}

const okJson = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

const headersOf = (init: RequestInit): Record<string, string> =>
  (init.headers ?? {}) as Record<string, string>;

afterEach(() => {
  vi.restoreAllMocks();
});

/* ----------------------------- Pre-approvals ---------------------------- */

describe("preapprovals api", () => {
  it("listPreapprovals hits GET /platform/wallets/{id}/preapprovals", async () => {
    const { calls, restore } = mockFetch(() => okJson([]));
    await api.listPreapprovals("d-1");
    expect(calls[0]!.url).toMatch(
      /\/platform\/wallets\/d-1\/preapprovals$/,
    );
    expect(calls[0]!.init.method ?? "GET").toBe("GET");
    restore();
  });

  it("setPreapproval issues PUT with body and Idempotency-Key", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ rule_id: "r1", max_amount_usdc: 10, used_count: 0 }),
    );
    await api.setPreapproval("d-1", "r1", {
      max_amount_usdc: 10,
      counterparty_category: "freelancer",
      max_uses: 3,
    });
    const call = calls[0]!;
    expect(call.url).toMatch(
      /\/platform\/wallets\/d-1\/preapprovals\/r1$/,
    );
    expect(call.init.method).toBe("PUT");
    const headers = headersOf(call.init);
    expect(headers["Idempotency-Key"]).toBeTruthy();
    expect(JSON.parse(String(call.init.body))).toMatchObject({
      max_amount_usdc: 10,
      counterparty_category: "freelancer",
      max_uses: 3,
    });
    restore();
  });

  it("revokePreapproval issues DELETE with Idempotency-Key", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ revoked: true, rule_id: "r1" }),
    );
    await api.revokePreapproval("d-1", "r1");
    expect(calls[0]!.init.method).toBe("DELETE");
    expect(headersOf(calls[0]!.init)["Idempotency-Key"]).toBeTruthy();
    restore();
  });
});

/* ----------------------------- Freelancers ----------------------------- */

describe("freelancers api", () => {
  it("listFreelancers hits operator route with owner_subject in path", async () => {
    const { calls, restore } = mockFetch(() => okJson([]));
    await api.listFreelancers("alice");
    expect(calls[0]!.url).toMatch(/\/platform\/freelancers\/alice$/);
    restore();
  });

  it("registerFreelancer POSTs to /platform/freelancers", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({
        owner_subject: "alice",
        freelancer_id: "f1",
        fiat_destination: "acct_x",
      }),
    );
    await api.registerFreelancer({
      owner_subject: "alice",
      freelancer_id: "f1",
      fiat_destination: "acct_x",
    });
    expect(calls[0]!.url).toMatch(/\/platform\/freelancers$/);
    expect(calls[0]!.init.method).toBe("POST");
    restore();
  });

  it("/me routes use the self-serve path", async () => {
    const { calls, restore } = mockFetch(() => okJson([]));
    await api.listMyFreelancers();
    expect(calls[0]!.url).toMatch(/\/me\/freelancers$/);

    const { calls: calls2, restore: r2 } = mockFetch(() =>
      okJson({
        owner_subject: "anonymous",
        freelancer_id: "f1",
        fiat_destination: "acct_x",
      }),
    );
    await api.registerMyFreelancer({
      owner_subject: "ignored",
      freelancer_id: "f1",
      fiat_destination: "acct_x",
    });
    expect(calls2[0]!.url).toMatch(/\/me\/freelancers$/);
    expect(calls2[0]!.init.method).toBe("POST");
    r2();

    const { calls: calls3, restore: r3 } = mockFetch(() =>
      okJson({ revoked: true, freelancer_id: "f1" }),
    );
    await api.revokeMyFreelancer("f1");
    expect(calls3[0]!.url).toMatch(/\/me\/freelancers\/f1$/);
    expect(calls3[0]!.init.method).toBe("DELETE");
    r3();
    restore();
  });
});

/* ----------------------------- Darwin payments ------------------------- */

describe("darwin fiat payment api", () => {
  it("darwinFiatPayment POSTs with body + idempotency", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ kind: "darwin_fiat_payout", amount_usdc: 10 }),
    );
    await api.darwinFiatPayment("d-1", {
      amount_usdc: 10,
      freelancer_id: "f1",
      escalation_id: "esc-99",
    });
    expect(calls[0]!.url).toMatch(/\/darwins\/d-1\/fiat_payment$/);
    expect(calls[0]!.init.method).toBe("POST");
    expect(headersOf(calls[0]!.init)["Idempotency-Key"]).toBeTruthy();
    expect(JSON.parse(String(calls[0]!.init.body))).toMatchObject({
      amount_usdc: 10,
      freelancer_id: "f1",
      escalation_id: "esc-99",
    });
    restore();
  });

  it("darwinFiatRefund hits the refund sub-route", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ kind: "refund_to_darwin", amount_usdc: 10 }),
    );
    await api.darwinFiatRefund("d-1", { amount_usdc: 10, reason: "ramp_outage" });
    expect(calls[0]!.url).toMatch(/\/darwins\/d-1\/fiat_payment\/refund$/);
    expect(calls[0]!.init.method).toBe("POST");
    restore();
  });

  it("retryOmnibusFund hits /darwins/{id}/omnibus/fund", async () => {
    const { calls, restore } = mockFetch(() =>
      okJson({ kind: "fund_child", amount_usdc: 10 }),
    );
    await api.retryOmnibusFund("d-1", { amount_usdc: 10 });
    expect(calls[0]!.url).toMatch(/\/darwins\/d-1\/omnibus\/fund$/);
    expect(calls[0]!.init.method).toBe("POST");
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({
      amount_usdc: 10,
    });
    restore();
  });
});
