import type { ReactNode } from "react";
import { Sidebar } from "@/components/layout/sidebar";
import { ApiBanner } from "@/components/layout/api-banner";

export default function DashboardLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <ApiBanner />
        <main className="flex-1 px-6 py-6">{children}</main>
      </div>
    </div>
  );
}
