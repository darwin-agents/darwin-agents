"use client";

import { useQuery } from "@tanstack/react-query";
import { Flag } from "lucide-react";
import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatRelative } from "@/lib/format";

export function EscalationList({ darwinId }: { darwinId: string }) {
  const { data, isLoading } = useQuery({
    queryKey: queryKeys.escalations(darwinId),
    queryFn: () => api.escalations(darwinId),
    refetchInterval: 30_000,
  });

  const tickets = (data?.tickets ?? []).filter((t) => t.status !== "resolved");

  return (
    <Card>
      <CardHeader>
        <CardTitle>Open escalations</CardTitle>
      </CardHeader>
      <CardContent className="px-0 pb-0">
        {isLoading ? (
          <p className="px-4 pb-4 text-sm text-[var(--color-muted-foreground)]">Loading…</p>
        ) : tickets.length === 0 ? (
          <p className="px-4 pb-4 text-sm text-[var(--color-muted-foreground)]">None.</p>
        ) : (
          <ul className="divide-y divide-[var(--color-border)]">
            {tickets.map((t) => (
              <li key={t.escalation_id} className="px-4 py-3">
                <div className="flex items-center gap-2 text-sm">
                  <Flag className="size-4 text-[var(--color-alert-pause)]" />
                  <span className="font-medium">{t.class ?? t.klass ?? "ticket"}</span>
                  <span className="text-[var(--color-muted-foreground)]">
                    · {t.summary ?? "(no summary)"}
                  </span>
                </div>
                <div className="mono mt-1 text-xs text-[var(--color-muted-foreground)]">
                  {t.escalation_id}
                  {t.deadline_at && <> · due {formatRelative(t.deadline_at)}</>}
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
