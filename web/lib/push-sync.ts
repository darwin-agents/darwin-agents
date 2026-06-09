/**
 * Push re-subscription glue (doc 22).
 *
 * Composition layer over `lib/push` (browser-side subscribe) and `lib/api`
 * (backend registration). It lives apart from `lib/push` so that module
 * stays free of the API client and remains independently testable — see
 * the note at the top of `lib/push.ts`.
 *
 * When a push service rotates a subscription it fires `pushsubscriptionchange`
 * inside the service worker. The SW can't re-publish to the backend itself
 * (the `/me/push-subscriptions` POST needs the owner's bearer token, which
 * the SW doesn't hold), so it `postMessage`s open windows and the client
 * re-registers here. The trade-off: if no tab is open when the endpoint
 * rotates, re-registration happens the next time the dashboard is opened —
 * `ServiceWorkerRegistrar` also runs a reconcile on mount for that case.
 */
import { ensurePushSubscription, getCurrentSubscription } from "./push";
import { api } from "./api";

function shortUa(): string {
  return typeof navigator !== "undefined" ? navigator.userAgent.slice(0, 200) : "";
}

/**
 * Re-publish the current browser push subscription to the backend. Returns
 * `true` when a subscription was (re-)registered, `false` when there's no
 * subscription to publish (permission revoked, or none ever created).
 *
 * `oldEndpoint`, when supplied and different from the fresh endpoint, is
 * revoked server-side so the rotated-away row doesn't linger.
 */
export async function resyncPushSubscription(
  oldEndpoint?: string | null,
): Promise<boolean> {
  const { public_key } = await api.getPushVapidPublicKey();
  const sub = await ensurePushSubscription(public_key);
  if (!sub) return false;
  await api.registerPushSubscription({
    endpoint: sub.endpoint,
    p256dh: sub.p256dh,
    auth: sub.auth,
    user_agent: shortUa(),
  });
  if (oldEndpoint && oldEndpoint !== sub.endpoint) {
    // Best-effort: the new row is what matters; a failed cleanup of the
    // stale endpoint shouldn't surface as a re-subscription failure.
    try {
      await api.revokePushSubscription({ endpoint: oldEndpoint });
    } catch {
      /* swallow */
    }
  }
  return true;
}

/**
 * On dashboard mount, re-publish an existing browser subscription if one is
 * live. Covers the "endpoint rotated while no tab was open" gap: the SW's
 * postMessage reached nobody, so we reconcile here. No-op when nothing is
 * subscribed (so we never prompt for permission on a cold load).
 */
export async function reconcilePushSubscriptionOnLoad(): Promise<void> {
  const current = await getCurrentSubscription();
  if (!current) return;
  try {
    await api.registerPushSubscription({
      endpoint: current.endpoint,
      p256dh: current.p256dh,
      auth: current.auth,
      user_agent: shortUa(),
    });
  } catch {
    /* best-effort reconcile */
  }
}
