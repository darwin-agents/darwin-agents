"use client";

import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, dedupeEvents } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { EventRow } from "./event-row";
import type { DarwinEvent } from "@/lib/types";

const PAGE_SIZE = 100;

const CATEGORY_FILTERS = [
  "all",
  "orchestrator",
  "neuron_dispatch",
  "llm_call",
  "payment",
  "escalation",
  "mutation",
  "reproduction",
  "owner_action",
  "platform",
  "death",
] as const;
type CategoryFilter = (typeof CATEGORY_FILTERS)[number];

export function EventTable({ darwinId }: { darwinId: string }) {
  const [offset, setOffset] = useState(0);
  const [category, setCategory] = useState<CategoryFilter>("all");
  // Cumulative window — newest-first. Polled page-0 results are merged in.
  const [window, setWindow] = useState<DarwinEvent[]>([]);

  const head = useQuery({
    queryKey: queryKeys.events(darwinId, 0),
    queryFn: () => api.events(darwinId, { limit: PAGE_SIZE, offset: 0 }),
    refetchInterval: () =>
      typeof document !== "undefined" && document.visibilityState === "visible" ? 3000 : false,
  });

  const older = useQuery({
    queryKey: queryKeys.events(darwinId, offset),
    queryFn: () => api.events(darwinId, { limit: PAGE_SIZE, offset }),
    enabled: offset > 0,
  });

  // Merge polled head + paged older into a single deduped window.
  useEffect(() => {
    setWindow((prev) => {
      const incoming = [
        ...(head.data?.events ?? []),
        ...(older.data?.events ?? []),
        ...prev,
      ];
      return dedupeEvents(incoming);
    });
  }, [head.data, older.data]);

  const filtered = useMemo(() => {
    if (category === "all") return window;
    return window.filter((ev) => {
      const raw = String(ev.category ?? ev.type ?? "").toLowerCase();
      return raw.includes(category);
    });
  }, [window, category]);

  const total = head.data?.total ?? 0;
  const hasMore = window.length < total;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle>Timeline</CardTitle>
          <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
            {window.length} of {total} events · 3s poll
          </p>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        <div className="flex flex-wrap items-center gap-1 border-b border-[var(--color-border)] px-3 py-2">
          {CATEGORY_FILTERS.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => setCategory(c)}
              className={
                category === c
                  ? "rounded-full bg-[var(--color-accent)] px-2.5 py-0.5 text-xs font-medium text-[var(--color-accent-foreground)]"
                  : "rounded-full px-2.5 py-0.5 text-xs text-[var(--color-muted-foreground)] hover:bg-[var(--color-muted)]"
              }
            >
              {c}
            </button>
          ))}
        </div>

        {head.isLoading && window.length === 0 ? (
          <div className="space-y-2 p-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : filtered.length === 0 ? (
          <p className="px-3 py-8 text-center text-sm text-[var(--color-muted-foreground)]">
            No events yet.
          </p>
        ) : (
          <div>
            {filtered.map((ev) => (
              <EventRow key={ev.event_id} ev={ev} />
            ))}
          </div>
        )}

        {hasMore && (
          <div className="flex justify-center border-t border-[var(--color-border)] p-3">
            <Button
              variant="outline"
              size="sm"
              disabled={older.isLoading}
              onClick={() => setOffset(window.length)}
            >
              {older.isLoading ? "Loading…" : "Load older"}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
