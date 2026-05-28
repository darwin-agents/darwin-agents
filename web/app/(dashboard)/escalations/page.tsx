"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Flag } from "lucide-react";
import { api } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatRelative, shortId } from "@/lib/format";
import { RespondDialog } from "@/components/darwin/respond-dialog";
import type { EscalationTicket } from "@/lib/types";

export default function EscalationsPage() {
  // Single aggregate fetch — backend already sorts by deadline.
  const { data, isLoading } = useQuery({
    queryKey: ["escalations", "fleet"] as const,
    queryFn: () => api.fleetEscalations(),
    refetchInterval: 30_000,
  });

  const tickets = (data?.tickets ?? []) as EscalationTicket[];
  const open = tickets
    .filter((t) => t.status === "open" || t.status === undefined)
    .map((ticket) => ({ darwinId: ticket.darwin_id, ticket }));
  const ids = Array.from(new Set(open.map((o) => o.darwinId)));

  const [active, setActive] = useState<EscalationTicket | null>(null);

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Escalations</h1>
        <p className="mt-1 text-sm text-[var(--color-muted-foreground)]">
          {isLoading
            ? "Loading…"
            : open.length === 0
              ? "Nothing waiting. ✓"
              : `${open.length} open across ${ids.length} Darwin${ids.length === 1 ? "" : "s"}`}
        </p>
      </header>

      {isLoading ? (
        <Skeleton className="h-24 w-full" />
      ) : open.length === 0 ? (
        <Card>
          <CardContent className="px-4 py-8 text-center text-sm text-[var(--color-muted-foreground)]">
            All clear.
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>Open tickets</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <ul className="divide-y divide-[var(--color-border)]">
              {open.map(({ darwinId, ticket }, idx) => (
                <li key={`${darwinId}:${ticket.ticket_id ?? idx}`}>
                  <button
                    type="button"
                    onClick={() => setActive(ticket)}
                    className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-[var(--color-muted)]/40"
                  >
                    <Flag className="size-4 text-[var(--color-alert-pause)]" />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 text-sm">
                        <span className="font-medium">
                          {ticket.ticket_class ?? "ticket"}
                        </span>
                        {ticket.neuron && (
                          <span className="mono text-xs text-[var(--color-muted-foreground)]">
                            {ticket.neuron}
                          </span>
                        )}
                        <span className="mono text-xs text-[var(--color-muted-foreground)]">
                          {shortId(darwinId, 8, 4)}
                        </span>
                      </div>
                      <p className="truncate text-xs text-[var(--color-muted-foreground)]">
                        {ticketSummary(ticket)}
                      </p>
                    </div>
                    <span className="text-xs text-[var(--color-muted-foreground)]">
                      {ticket.deadline_ts ? `due ${formatRelative(ticket.deadline_ts)}` : "—"}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      <RespondDialog
        ticket={active}
        open={!!active}
        onOpenChange={(o) => {
          if (!o) setActive(null);
        }}
      />
    </div>
  );
}

function ticketSummary(t: { ticket_class?: string; detail?: Record<string, unknown> }): string {
  const detail = t.detail ?? {};
  const candidates = ["summary", "message", "reason", "title"];
  for (const k of candidates) {
    const v = detail[k];
    if (typeof v === "string" && v.trim()) return v;
  }
  return t.ticket_class ? `${t.ticket_class} ticket` : "(no summary)";
}
