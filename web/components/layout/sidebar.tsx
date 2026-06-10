"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Bell,
  FlaskConical,
  Gauge,
  Inbox,
  LayoutGrid,
  Receipt,
  Users,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { AuthChip } from "./auth-chip";

type NavItem = {
  href: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  operatorOnly?: boolean;
};

const NAV: NavItem[] = [
  { href: "/", label: "Fleet", icon: LayoutGrid },
  { href: "/escalations", label: "Escalations", icon: Inbox },
  { href: "/freelancers", label: "Freelancers", icon: Users },
  { href: "/operator", label: "Operator", icon: Gauge, operatorOnly: true },
  { href: "/notifications", label: "Notifications", icon: Bell },
  { href: "/tokens", label: "Design tokens", icon: FlaskConical },
];

export function Sidebar() {
  const pathname = usePathname();
  const { data: caps } = useQuery({
    queryKey: queryKeys.capabilities(),
    queryFn: () => api.capabilities(),
    staleTime: 5 * 60_000,
    retry: false,
  });
  const items = NAV.filter((item) => !item.operatorOnly || caps?.is_operator);
  return (
    <aside className="hidden w-56 flex-col border-r border-[var(--color-border)] bg-[var(--color-card)] p-3 md:flex">
      <div className="flex items-center gap-2 px-2 py-3">
        <Activity className="size-5 text-[var(--color-status-alive)]" />
        <span className="text-sm font-semibold tracking-tight">Darwin</span>
      </div>
      <nav className="mt-2 flex flex-col gap-1">
        {items.map((item) => {
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
        </div>
      </div>
    </aside>
  );
}
