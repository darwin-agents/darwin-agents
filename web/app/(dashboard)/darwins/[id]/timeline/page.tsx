"use client";

import { use } from "react";
import { useSearchParams } from "next/navigation";
import { DarwinHeader } from "@/components/darwin/darwin-header";
import { EventTable } from "@/components/timeline/event-table";
import {
  TimelineFilterBar,
  parseFilters,
} from "@/components/timeline/filter-bar";

/**
 * Full-page timeline view (M3). URL-encoded filters via `?kind=`, `?neuron=`,
 * `?tier=` — any view can be shared as a link. The same EventTable component
 * powers the inline feed on the Darwin shell; this page just hands it the
 * URL-derived filter set.
 */
export default function TimelinePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const sp = useSearchParams();
  const f = parseFilters(new URLSearchParams(sp.toString()));

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4">
      <DarwinHeader darwinId={id} />
      <TimelineFilterBar />
      <EventTable
        darwinId={id}
        filters={{
          kind: f.kind || undefined,
          neuron_id: f.neuron || undefined,
          tier: f.tier || undefined,
        }}
      />
    </div>
  );
}
