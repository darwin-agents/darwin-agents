import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock the two halves push-sync composes so we can assert the orchestration
// without a real PushManager or backend.
vi.mock("@/lib/push", () => ({
  ensurePushSubscription: vi.fn(),
  getCurrentSubscription: vi.fn(),
}));
vi.mock("@/lib/api", () => ({
  api: {
    getPushVapidPublicKey: vi.fn(),
    registerPushSubscription: vi.fn(),
    revokePushSubscription: vi.fn(),
  },
}));

import { ensurePushSubscription, getCurrentSubscription } from "@/lib/push";
import { api } from "@/lib/api";
import {
  reconcilePushSubscriptionOnLoad,
  resyncPushSubscription,
} from "@/lib/push-sync";

const SUB = {
  endpoint: "https://push.example/new",
  p256dh: "p256dh-key",
  auth: "auth-key",
  expirationTime: null,
};

beforeEach(() => {
  vi.mocked(api.getPushVapidPublicKey).mockResolvedValue({
    public_key: "vapid-public",
  } as never);
  vi.mocked(api.registerPushSubscription).mockResolvedValue({} as never);
  vi.mocked(api.revokePushSubscription).mockResolvedValue({} as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("resyncPushSubscription", () => {
  it("fetches the VAPID key, re-subscribes, and re-registers the new endpoint", async () => {
    vi.mocked(ensurePushSubscription).mockResolvedValue(SUB);

    const ok = await resyncPushSubscription(null);

    expect(ok).toBe(true);
    expect(api.getPushVapidPublicKey).toHaveBeenCalledOnce();
    expect(ensurePushSubscription).toHaveBeenCalledWith("vapid-public");
    expect(api.registerPushSubscription).toHaveBeenCalledWith(
      expect.objectContaining({
        endpoint: SUB.endpoint,
        p256dh: SUB.p256dh,
        auth: SUB.auth,
      }),
    );
  });

  it("revokes the rotated-away endpoint when it differs from the new one", async () => {
    vi.mocked(ensurePushSubscription).mockResolvedValue(SUB);

    await resyncPushSubscription("https://push.example/old");

    expect(api.revokePushSubscription).toHaveBeenCalledWith({
      endpoint: "https://push.example/old",
    });
  });

  it("does not revoke when the old endpoint equals the new endpoint", async () => {
    vi.mocked(ensurePushSubscription).mockResolvedValue(SUB);

    await resyncPushSubscription(SUB.endpoint);

    expect(api.revokePushSubscription).not.toHaveBeenCalled();
  });

  it("returns false and skips registration when permission is denied (no sub)", async () => {
    vi.mocked(ensurePushSubscription).mockResolvedValue(null);

    const ok = await resyncPushSubscription(null);

    expect(ok).toBe(false);
    expect(api.registerPushSubscription).not.toHaveBeenCalled();
  });

  it("treats a failed stale-endpoint revoke as best-effort (still resolves true)", async () => {
    vi.mocked(ensurePushSubscription).mockResolvedValue(SUB);
    vi.mocked(api.revokePushSubscription).mockRejectedValue(new Error("gone"));

    await expect(
      resyncPushSubscription("https://push.example/old"),
    ).resolves.toBe(true);
  });
});

describe("reconcilePushSubscriptionOnLoad", () => {
  it("re-registers an already-live subscription on mount", async () => {
    vi.mocked(getCurrentSubscription).mockResolvedValue(SUB);

    await reconcilePushSubscriptionOnLoad();

    expect(api.registerPushSubscription).toHaveBeenCalledWith(
      expect.objectContaining({ endpoint: SUB.endpoint }),
    );
  });

  it("is a no-op (no permission prompt) when nothing is subscribed", async () => {
    vi.mocked(getCurrentSubscription).mockResolvedValue(null);

    await reconcilePushSubscriptionOnLoad();

    expect(api.getPushVapidPublicKey).not.toHaveBeenCalled();
    expect(api.registerPushSubscription).not.toHaveBeenCalled();
  });
});
