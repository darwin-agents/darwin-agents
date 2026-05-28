"use client";

import { useEffect, useMemo, useRef, useState } from "react";
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
  "neuron",
  "llm",
  "payment",
  "escalation",
  "mutation",
  "reproduction",
  "owner",
  "platform",
  "death",
] as const;
type CategoryFilter = (typeof CATEGORY_FILTERS)[number];

/**
 * Newest-first event feed. First page comes from
 * `GET /events?limit=N` (head); polling uses `?since=` to fetch only
 * deltas, eliminating the page-0 re-download. Older pages load on demand
 * via `?cursor=`.
 */
export function EventTable({ darwinId }: { darwinId: string }) {
  const [category, setCategory] = useState<CategoryFilter>("all");
  const [window, setWindow] = useState<DarwinEvent[]>([]);
  const headSeqRef = useRef<number | undefined>(undefined);
  const tailSeqRef = useRef<number | undefined>(undefined);
  const [oldestLoading, setOldestLoading] = useState(false);
  const [hasMoreOlder, setHasMoreOlder] = useState(true);

  // Initial head fetch — newest-first, no cursor.
  const head = useQuery({
    queryKey: queryKeys.events(darwinId, 0),
    queryFn: () => api.events(darwinId, { limit: PAGE_SIZE }),
  });

  useEffect(() => {
    const evs = head.data?.events ?? [];
    if (evs.length === 0) return;
    setWindow((prev) => dedupeEvents([...evs, ...prev]));
    headSeqRef.current = Math.max(...evs.map((e) => (e as DarwinEvent).seq ?? 0));
    tailSeqRef.current = Math.min(...evs.map((e) => (e as DarwinEvent).seq ?? Infinity));
  }, [head.data]);

  // Tail-cursor polling — only events newer than the head we've seen.
  useQuery({
    queryKey: ["darwin", darwinId, "events", "since", headSeqRef.current ?? -1] as const,
    enabled: !!head.data && headSeqRef.current !== undefined,
    queryFn: async () => {
      const res = await api.events(darwinId, {
        since: headSeqRef.current!,
        limit: PAGE_SIZE,
      });
      const evs = res.events ?? [];
      if (evs.length > 0) {
        setWindow((prev) => dedupeEvents([...evs.slice().reverse(), ...prev]));
        headSeqRef.current = Math.max(
          headSeqRef.current ?? 0,
          ...evs.map((e) => (e as DarwinEvent).seq ?? 0),
        );
      }
      return res;
    },
    refetchInterval: () =>
      typeof document !== "undefined" && document.visibilityState === "visible" ? 3000 : false,
  });

  const filtered = useMemo(() => {
    if (category === "all") return window;
    return window.filter((ev) => {
      const raw = String(ev.kind ?? ev.category ?? ev.type ?? "").toLowerCase();
      return raw.includes(category);
    });
  }, [window, category]);

  const total = head.data?.total ?? 0;

  const loadOlder = async () => {
    if (tailSeqRef.current === undefined || oldestLoading) return;
    setOldestLoading(true);
    try {
      const res = await api.events(darwinId, {
        cursor: tailSeqRef.current,
        limit: PAGE_SIZE,
      });
      const evs = res.events ?? [];
      if (evs.length === 0) {
        setHasMoreOlder(false);
        return;
      }
      setWindow((prev) => dedupeEvents([...prev, ...evs]));
      tailSeqRef.current = Math.min(
        tailSeqRef.current ?? Infinity,
        ...evs.map((e) => (e as DarwinEvent).seq ?? Infinity),
      );
      if (res.next_cursor === null || res.next_cursor === undefined) {
        setHasMoreOlder(false);
      }
    } finally {
      setOldestLoading(false);
    }
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle>Timeline</CardTitle>
          <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
            {window.length} of {total} events · 3s tail poll
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
              <EventRow key={ev.seq ?? ev.event_id ?? ev.hash} ev={ev} />
            ))}
          </div>
        )}

        {hasMoreOlder && (
          <div className="flex justify-center border-t border-[var(--color-border)] p-3">
            <Button variant="outline" size="sm" disabled={oldestLoading} onClick={loadOlder}>
              {oldestLoading ? "Loading…" : "Load older"}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
