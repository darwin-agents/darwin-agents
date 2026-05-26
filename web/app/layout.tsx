import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { QueryProvider } from "@/providers/query-provider";
import { DevAuthProvider } from "@/providers/dev-auth-provider";

export const metadata: Metadata = {
  title: "Darwin",
  description: "Own a Darwin without operating it.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-[var(--color-background)] text-[var(--color-foreground)] antialiased">
        <QueryProvider>
          <DevAuthProvider>{children}</DevAuthProvider>
        </QueryProvider>
      </body>
    </html>
  );
}
