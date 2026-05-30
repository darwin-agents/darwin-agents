import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { QueryProvider } from "@/providers/query-provider";
import { AuthProvider } from "@/providers/auth-provider";
import { ServiceWorkerRegistrar } from "@/components/pwa/service-worker-registrar";

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
