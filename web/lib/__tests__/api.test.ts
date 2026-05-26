import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch, ApiError, dedupeEvents } from "../api";

describe("apiFetch", () => {
  const originalFetch = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it("surfaces 4xx as ApiError with code from detail", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "not your darwin" }), {
        status: 403,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await expect(
      apiFetch("/darwins/x", { token: "tok" }),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 403,
      code: "not your darwin",
    });
  });

  it("adds Idempotency-Key on mutating POSTs", async () => {
    const spy = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({}), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    globalThis.fetch = spy as unknown as typeof fetch;
    await apiFetch("/darwins/x/kill", {
      method: "POST",
      body: { reason: "test" },
      idempotencyKey: "fixed-key",
      token: "tok",
    });
    const args = spy.mock.calls[0];
    const init = args?.[1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers["Idempotency-Key"]).toBe("fixed-key");
    expect(headers["Authorization"]).toBe("Bearer tok");
  });
});

describe("ApiError shape", () => {
  it("classifies common statuses", () => {
    const e = new ApiError(404, "not_found", null);
    expect(e.isNotFound).toBe(true);
    expect(e.isAuth).toBe(false);
  });
});

describe("dedupeEvents", () => {
  it("drops duplicates by event_id and preserves order", () => {
    const a = { event_id: "a", ts: "1" };
    const b = { event_id: "b", ts: "2" };
    const a2 = { event_id: "a", ts: "3" };
    expect(dedupeEvents([a, b, a2])).toEqual([a, b]);
  });
});
