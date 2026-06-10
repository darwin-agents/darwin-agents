import type { NextConfig } from "next";

// NOTE: The Content-Security-Policy is set per-request in `middleware.ts`
// (not here) so each HTML response carries a fresh nonce. A static CSP
// can't nonce Next's inline bootstrap/hydration scripts, so in production
// a `script-src 'self'` header here would block the App Router's own
// inline scripts and the app would never hydrate. The non-nonce security
// headers stay static below.

const nextConfig: NextConfig = {
  // Emit a self-contained server bundle (.next/standalone/server.js) so the
  // Docker image ships only what the server needs — no full node_modules.
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  typedRoutes: true,
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
        ],
      },
    ];
  },
};

export default nextConfig;
