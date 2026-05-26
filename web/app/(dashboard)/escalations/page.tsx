"use client";

import Link from "next/link";
import { useQuery, useQueries } from "@tanstack/react-query";
import { Flag } from "lucide-react";
import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatRelative, shortId } from "@/lib/format";

/**
 * Cross-Darwin escalation inbox — fans `GET /darwins/{id}/escalations` out
 * across every Darwin the operator owns. M2-B aggregate endpoint (backend
 * register #5) would collapse this into a single request.
 */
export default function EscalationsPage() {
  const { data: list, isLoading } = useQuery({
    queryKey: queryKeys.darwins(),
    queryFn: () => api.listDarwins(),
    refetchInterval: 60_000,
  });

  const ids = list?.darwins ?? [];

  const escalations = useQueries({
    queries: ids.map((id) => ({
      queryKey: queryKeys.escalations(id),
      queryFn: () => api.escalations(id),
      refetchInterval: 30_000,
    })),
  });

  const open = ids
    .flatMap((id, i) =>
      (escalations[i]?.data?.tickets ?? []).map((t) => ({ darwinId: id, ticket: t })),
    )
    .filter(({ ticket }) => ticket.status !== "resolved")
    .sort((a, b) => {
      const ad = a.ticket.deadline_at ? new Date(a.ticket.deadline_at).getTime() : Infinity;
      const bd = b.ticket.deadline_at ? new Date(b.ticket.deadline_at).getTime() : Infinity;
      return ad - bd;
    });

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
              {open.map(({ darwinId, ticket }) => (
                <li key={ticket.escalation_id}>
                  <Link
                    href={{ pathname: `/darwins/${darwinId}` }}
                    className="flex items-center gap-3 px-4 py-3 hover:bg-[var(--color-muted)]/40"
                  >
                    <Flag className="size-4 text-[var(--color-alert-pause)]" />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 text-sm">
                        <span className="font-medium">
                          {ticket.class ?? ticket.klass ?? "ticket"}
                        </span>
                        <span className="mono text-xs text-[var(--color-muted-foreground)]">
                          {shortId(darwinId, 8, 4)}
                        </span>
                      </div>
                      <p className="truncate text-xs text-[var(--color-muted-foreground)]">
                        {ticket.summary ?? "(no summary)"}
                      </p>
                    </div>
                    <span className="text-xs text-[var(--color-muted-foreground)]">
                      {ticket.deadline_at ? `due ${formatRelative(ticket.deadline_at)}` : "—"}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
