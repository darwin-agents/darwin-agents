import type { ReactNode } from "react";
import { MobileNav, Sidebar } from "@/components/layout/sidebar";
import { ApiBanner } from "@/components/layout/api-banner";

export default function DashboardLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <MobileNav />
        <ApiBanner />
        <main className="flex-1 px-4 py-6 md:px-6">{children}</main>
      </div>
    </div>
  );
}
