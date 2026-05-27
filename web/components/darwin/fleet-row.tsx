"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { formatRelative, formatUsdc, shortId } from "@/lib/format";
import { StatusBadge } from "@/components/darwin/status-badge";
import { AlertBadges } from "@/components/darwin/alert-badges";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * One row of the fleet table. Each Darwin self-fetches status + alerts +
 * escalations because `GET /darwins` returns only ids today (see
 * docs/frontend/10 §M1-A). The N+1 is acceptable for the typical owner with
 * 1-3 Darwins; the aggregate endpoint is backend register #5.
 */
export function FleetRow({ darwinId }: { darwinId: string }) {
  const { data: status, isLoading: statusLoading } = useQuery({
    queryKey: queryKeys.darwin(darwinId),
    queryFn: () => api.getDarwin(darwinId),
    refetchInterval: 15_000,
  });

  const { data: alerts } = useQuery({
    queryKey: queryKeys.alerts(darwinId),
    queryFn: () => api.alerts(darwinId),
    refetchInterval: 30_000,
  });

  const { data: escalations } = useQuery({
    queryKey: queryKeys.escalations(darwinId),
    queryFn: () => api.escalations(darwinId),
    refetchInterval: 30_000,
  });

  const balance =
    (status?.balance_usdc as number | undefined) ?? (status?.treasury_usdc as number | undefined);
  const openCount =
    escalations?.tickets?.filter((t) => t.status === "open" || t.status === undefined).length ?? 0;
  const heartbeat = status?.last_heartbeat_at;

  return (
    <Link
      href={{ pathname: `/darwins/${darwinId}` }}
      className="grid grid-cols-[minmax(0,1fr)_auto_auto_auto_auto_auto] items-center gap-4 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-4 py-3 transition-colors hover:bg-[var(--color-muted)]/50"
    >
      <div className="min-w-0">
        <div className="mono truncate text-sm font-medium">{shortId(darwinId, 10, 6)}</div>
        <div className="mt-1 flex items-center gap-2 text-xs text-[var(--color-muted-foreground)]">
          {statusLoading ? (
            <Skeleton className="h-3 w-24" />
          ) : (
            <>
              <span>gen {status?.generation ?? "—"}</span>
              {heartbeat && <span>· heartbeat {formatRelative(heartbeat)}</span>}
            </>
          )}
        </div>
      </div>

      <AlertBadges alerts={alerts?.alerts} />

      <div className="text-right text-sm tabular-nums">
        {balance !== undefined ? formatUsdc(balance) : "—"}
      </div>

      <div className="w-20 text-right text-xs text-[var(--color-muted-foreground)]">
        {openCount > 0 ? (
          <span className="text-[var(--color-alert-pause)]">
            {openCount} open escalation{openCount === 1 ? "" : "s"}
          </span>
        ) : (
          <span>—</span>
        )}
      </div>

      <div className="w-28 justify-self-end">
        <StatusBadge status={status?.lifecycle ?? "alive"} />
      </div>

      <ChevronRight className="size-4 text-[var(--color-muted-foreground)]" />
    </Link>
  );
}
