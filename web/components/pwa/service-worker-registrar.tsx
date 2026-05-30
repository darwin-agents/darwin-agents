"use client";

/**
 * Side-effect-only client component that registers `/sw.js` once the page
 * mounts. Idempotent — the browser dedupes by scope.
 *
 * Wrapping the registration in a component (vs inline in layout.tsx) keeps
 * the root layout a server component and gives us a single place to add
 * follow-up wiring (e.g. listening for `pushsubscriptionchange` messages
 * the SW posts back when an autopush endpoint rotates).
 */
import { useEffect } from "react";

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
    return () => {
      // Don't unregister on unmount — the SW is intentionally global.
      void reg;
    };
  }, []);
  return null;
}
