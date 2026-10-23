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
  Menu,
  Receipt,
  Rocket,
  Users,
} from "lucide-react";
import { CLERK_ENABLED } from "@/lib/auth-mode";
import { cn } from "@/lib/cn";
import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { AuthChip } from "./auth-chip";

type NavItem = {
  href: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  operatorOnly?: boolean;
  devOnly?: boolean;
};

const NAV: NavItem[] = [
  { href: "/", label: "Fleet", icon: LayoutGrid },
  { href: "/onboarding", label: "Get started", icon: Rocket },
  { href: "/escalations", label: "Escalations", icon: Inbox },
  { href: "/freelancers", label: "Freelancers", icon: Users },
  { href: "/operator", label: "Operator", icon: Gauge, operatorOnly: true },
  { href: "/notifications", label: "Notifications", icon: Bell },
  // The dev token-paste page; Clerk replaces it in production.
  { href: "/tokens", label: "Dev tokens", icon: FlaskConical, devOnly: true },
];

const VERSION = process.env.NEXT_PUBLIC_APP_VERSION ?? "dev";

function useNavItems() {
  const { data: caps } = useQuery({
    queryKey: queryKeys.capabilities(),
    queryFn: () => api.capabilities(),
    staleTime: 5 * 60_000,
    retry: false,
  });
  return NAV.filter(
    (item) => (!item.operatorOnly || caps?.is_operator) && (!item.devOnly || !CLERK_ENABLED),
  );
}

/** Small screens: a top bar with the same links behind a native disclosure. */
export function MobileNav() {
  const items = useNavItems();
  return (
    <details className="border-b border-[var(--color-border)] bg-[var(--color-card)] md:hidden">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-sm font-semibold">
        <Menu className="size-4" />
        <Activity className="size-4 text-[var(--color-status-alive)]" />
        Darwin
      </summary>
      <nav className="flex flex-col gap-1 px-2 pb-3">
        {items.map((item) => (
          <Link
            key={item.href}
            href={item.href as never}
            className="flex items-center gap-2 rounded-md px-2 py-2 text-sm text-[var(--color-muted-foreground)] hover:bg-[var(--color-muted)]"
          >
            <item.icon className="size-4" />
            {item.label}
          </Link>
        ))}
        <div className="px-2 pt-2">
          <AuthChip />
        </div>
      </nav>
    </details>
  );
}

export function Sidebar() {
  const pathname = usePathname();
  const items = useNavItems();
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
            <span>{VERSION}</span>
          </div>
        </div>
      </div>
    </aside>
  );
}
