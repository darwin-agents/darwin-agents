import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { QueryProvider } from "@/providers/query-provider";
import { AuthProvider } from "@/providers/auth-provider";
import { ServiceWorkerRegistrar } from "@/components/pwa/service-worker-registrar";

// Render every route per-request so the CSP nonce that `middleware.ts`
// generates is injected into Next's inline bootstrap scripts. A statically
// prerendered document is baked at build time with no nonce, so its inline
// scripts get blocked by the strict `script-src`. The dashboard is
// auth-gated and live-data driven, so there's nothing to statically cache
// anyway.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Darwin",
  description: "Own a Darwin without operating it.",
  manifest: "/manifest.webmanifest",
  applicationName: "Darwin",
  appleWebApp: {
    capable: true,
    title: "Darwin",
    statusBarStyle: "black-translucent",
  },
  icons: {
    icon: [
      { url: "/icons/icon-192.svg", sizes: "192x192", type: "image/svg+xml" },
    ],
    apple: [
      { url: "/icons/icon-192.svg", sizes: "192x192", type: "image/svg+xml" },
    ],
  },
};

export const viewport: Viewport = {
  themeColor: "#04080F",
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <body
        className="min-h-screen bg-[var(--color-background)] text-[var(--color-foreground)] antialiased"
        suppressHydrationWarning
      >
        <QueryProvider>
          <AuthProvider>
            <ServiceWorkerRegistrar />
            {children}
          </AuthProvider>
        </QueryProvider>
      </body>
    </html>
  );
}
