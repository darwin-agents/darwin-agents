"use client";

/**
 * Side-effect-only client component that registers `/sw.js` once the page
 * mounts. Idempotent — the browser dedupes by scope.
 *
 * It also owns the push-subscription rotation wiring:
 *  - listens for the `push-subscription-change` message the SW posts when an
 *    autopush endpoint rotates, and re-publishes the new endpoint;
 *  - reconciles once on mount, covering a rotation that happened while no
 *    tab was open (so the SW's postMessage reached nobody).
 */
import { useEffect } from "react";
import {
  reconcilePushSubscriptionOnLoad,
  resyncPushSubscription,
} from "@/lib/push-sync";

export function ServiceWorkerRegistrar() {
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!("serviceWorker" in navigator)) return;
    // Don't register during E2E (Playwright) — the SW caches the index.html
    // for offline-first behaviour the smoke tests don't expect.
    if (
      navigator.userAgent.includes("HeadlessChrome") &&
      process.env.NEXT_PUBLIC_DISABLE_SW === "1"
    ) {
      return;
    }
    const reg = navigator.serviceWorker
      .register("/sw.js", { scope: "/" })
      .catch((err) => {
        // SW errors are observable in devtools; surface in console too.
        console.warn("[darwin] service worker registration failed", err);
      });

    // Catch a rotation that happened while no tab was open.
    void reconcilePushSubscriptionOnLoad();

    // Re-register on live rotation messages from the SW.
    const onMessage = (event: MessageEvent) => {
      const data = event.data as
        | { type?: string; oldEndpoint?: string | null }
        | undefined;
      if (!data || data.type !== "push-subscription-change") return;
      void resyncPushSubscription(data.oldEndpoint).catch((err) => {
        console.warn("[darwin] push re-subscription failed", err);
      });
    };
    navigator.serviceWorker.addEventListener("message", onMessage);

    return () => {
      // Don't unregister on unmount — the SW is intentionally global.
      void reg;
      navigator.serviceWorker.removeEventListener("message", onMessage);
    };
  }, []);
  return null;
}
