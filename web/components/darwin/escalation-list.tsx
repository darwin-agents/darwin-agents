"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Flag } from "lucide-react";
import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatRelative } from "@/lib/format";
import { RespondDialog } from "@/components/darwin/respond-dialog";
import type { EscalationTicket } from "@/lib/types";

export function EscalationList({ darwinId }: { darwinId: string }) {
  const { data, isLoading } = useQuery({
    queryKey: queryKeys.escalations(darwinId),
    queryFn: () => api.escalations(darwinId),
    refetchInterval: 30_000,
  });

  const [active, setActive] = useState<EscalationTicket | null>(null);
  const tickets = (data?.tickets ?? []).filter(
    (t) => t.status === "open" || t.status === undefined,
  );

  return (
    <>
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
              {tickets.map((t, idx) => (
                <li key={t.ticket_id ?? `t-${idx}`}>
                  <button
                    type="button"
                    onClick={() => setActive(t)}
                    className="w-full px-4 py-3 text-left transition-colors hover:bg-[var(--color-muted)]/40"
                  >
                    <div className="flex items-center gap-2 text-sm">
                      <Flag className="size-4 text-[var(--color-alert-pause)]" />
                      <span className="font-medium">{t.ticket_class ?? "ticket"}</span>
                      {t.neuron && (
                        <span className="mono text-xs text-[var(--color-muted-foreground)]">
                          · {t.neuron}
                        </span>
                      )}
                    </div>
                    <div className="mono mt-1 text-xs text-[var(--color-muted-foreground)]">
                      {t.ticket_id}
                      {t.deadline_ts !== undefined && <> · due {formatRelative(t.deadline_ts)}</>}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <RespondDialog
        ticket={active}
        open={!!active}
        onOpenChange={(o) => {
          if (!o) setActive(null);
        }}
      />
    </>
  );
}
