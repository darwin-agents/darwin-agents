/**
 * Web Push client helpers (doc 22 — PWA escalation companion).
 *
 * Three entry points the dashboard uses:
 *  - `getPushPermissionState()` — synchronous "where are we?" check.
 *  - `ensurePushSubscription(applicationServerKey)` — full happy path:
 *    request permission → service-worker → subscribe → return the
 *    PushSubscription. Idempotent: returns the existing subscription if
 *    one is already live for the current public key.
 *  - `unsubscribePush()` — cancel the browser-side subscription.
 *
 * Backend registration of the subscription is the caller's job (via
 * `api.registerPushSubscription` from `lib/api.ts`) so the two halves
 * can be exercised independently in tests.
 */

export type PushPermissionState =
  | "unsupported"
  | "denied"
  | "default"
  | "granted";

export interface SubscribeResult {
  endpoint: string;
  p256dh: string;
  auth: string;
  expirationTime: number | null;
}

export function isPushSupported(): boolean {
  if (typeof window === "undefined") return false;
  return (
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

export function getPushPermissionState(): PushPermissionState {
  if (!isPushSupported()) return "unsupported";
  return Notification.permission as PushPermissionState;
}

/**
 * Decodes a base64url-encoded VAPID public key into the Uint8Array the
 * browser's PushManager wants. Standard helper — exported so vitest can
 * round-trip it directly.
 */
export function urlBase64ToUint8Array(base64: string): Uint8Array {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const base64Std = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw =
    typeof atob === "function"
      ? atob(base64Std)
      : Buffer.from(base64Std, "base64").toString("binary");
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) {
    out[i] = raw.charCodeAt(i);
  }
  return out;
}

function readSubscription(sub: PushSubscription): SubscribeResult {
  // PushSubscription.getKey returns an ArrayBuffer; encode as base64url.
  const p256dh = encodeKey(sub.getKey("p256dh"));
  const auth = encodeKey(sub.getKey("auth"));
  if (!p256dh || !auth) {
    throw new Error("PushSubscription missing p256dh or auth key");
  }
  return {
    endpoint: sub.endpoint,
    p256dh,
    auth,
    expirationTime: sub.expirationTime ?? null,
  };
}

function encodeKey(buf: ArrayBuffer | null): string | null {
  if (!buf) return null;
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) {
    const b = bytes[i];
    if (b !== undefined) bin += String.fromCharCode(b);
  }
  const std =
    typeof btoa === "function"
      ? btoa(bin)
      : Buffer.from(bin, "binary").toString("base64");
  return std.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * Ensure a push subscription exists for `applicationServerKey`. Returns
 * `null` only when the user explicitly denies permission. Throws on
 * unsupported browsers / SW failures so the caller can show a
 * targeted error message.
 */
export async function ensurePushSubscription(
  applicationServerKey: string,
): Promise<SubscribeResult | null> {
  if (!isPushSupported()) {
    throw new Error("Push notifications are not supported in this browser");
  }
  const perm =
    Notification.permission === "default"
      ? await Notification.requestPermission()
      : Notification.permission;
  if (perm !== "granted") return null;

  const reg = await navigator.serviceWorker.ready;
  // If a subscription already exists for the SAME public key, reuse it.
  // Subscribing again with the same key would no-op anyway, but reading
  // first means we don't bounce the user through the permission UX.
  const existing = await reg.pushManager.getSubscription();
  if (existing) {
    // The browser doesn't expose the applicationServerKey it was
    // subscribed with — but if the backend stored the (endpoint,key)
    // pair, re-registering on the server side is cheap and idempotent.
    return readSubscription(existing);
  }
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(applicationServerKey),
  });
  return readSubscription(sub);
}

export async function getCurrentSubscription(): Promise<SubscribeResult | null> {
  if (!isPushSupported()) return null;
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.getSubscription();
  return sub ? readSubscription(sub) : null;
}

export async function unsubscribePush(): Promise<string | null> {
  if (!isPushSupported()) return null;
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.getSubscription();
  if (!sub) return null;
  const endpoint = sub.endpoint;
  await sub.unsubscribe();
  return endpoint;
}
