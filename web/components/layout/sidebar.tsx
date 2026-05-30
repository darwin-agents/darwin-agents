"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  Bell,
  FlaskConical,
  Inbox,
  LayoutGrid,
  Receipt,
  Users,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { AuthChip } from "./auth-chip";

const NAV: Array<{ href: string; label: string; icon: React.ComponentType<{ className?: string }> }> = [
  { href: "/", label: "Fleet", icon: LayoutGrid },
  { href: "/escalations", label: "Escalations", icon: Inbox },
  { href: "/freelancers", label: "Freelancers", icon: Users },
  { href: "/tokens", label: "Design tokens", icon: FlaskConical },
];

export function Sidebar() {
  const pathname = usePathname();
  return (
    <aside className="hidden w-56 flex-col border-r border-[var(--color-border)] bg-[var(--color-card)] p-3 md:flex">
      <div className="flex items-center gap-2 px-2 py-3">
        <Activity className="size-5 text-[var(--color-status-alive)]" />
        <span className="text-sm font-semibold tracking-tight">Darwin</span>
      </div>
      <nav className="mt-2 flex flex-col gap-1">
        {NAV.map((item) => {
          const active = pathname === item.href || pathname.startsWith(item.href + "/");
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href as never}
              className={cn(
                "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm transition-colors",
                active
                  ? "bg-[var(--color-muted)] text-[var(--color-foreground)]"
                  : "text-[var(--color-muted-foreground)] hover:bg-[var(--color-muted)]/60 hover:text-[var(--color-foreground)]",
              )}
            >
              <Icon className="size-4" />
              {item.label}
            </Link>
          );
        })}
      </nav>
      <div className="mt-auto flex flex-col gap-2 px-2">
        <AuthChip />
        <div className="text-[10px] text-[var(--color-muted-foreground)]">
          <div className="flex items-center gap-1">
            <Receipt className="size-3" />
            <span>v0.1.0 · dev</span>
          </div>
          <div className="mt-1 flex items-center gap-1">
            <Bell className="size-3" />
            <span>WS poll fallback</span>
          </div>
        </div>
      </div>
    </aside>
  );
}
