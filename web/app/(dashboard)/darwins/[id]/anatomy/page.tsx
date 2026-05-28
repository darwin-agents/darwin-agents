"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Pause, Play, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import { DarwinStream, type StreamMessage, type ConnectionState } from "@/lib/ws";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { AnatomyCanvas, type AnatomySnapshot } from "@/components/anatomy/anatomy-canvas";
import { formatRelative } from "@/lib/format";

export default function AnatomyPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);

  // Initial snapshot via REST. WS replaces this with live patches.
  const initial = useQuery({
    queryKey: ["darwin", id, "anatomy"] as const,
    queryFn: () => api.anatomy(id),
    // Stop polling once WS is connected (idempotent — refetch on focus
    // covers the brief disconnect windows during reconnect backoff).
    refetchInterval: (q) => (q.state.data ? false : 5000),
  });

  const [snapshot, setSnapshot] = useState<AnatomySnapshot | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("idle");
  const [selectedNeuronId, setSelectedNeuronId] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    if (!snapshot && initial.data) setSnapshot(initial.data as AnatomySnapshot);
  }, [initial.data, snapshot]);

  // Open the WS as soon as we have an initial snapshot to swap into.
  useEffect(() => {
    if (!initial.data) return;
    const stream = new DarwinStream(id, {
      channels: ["anatomy", "status"],
      onStateChange: setConnection,
      onMessage: (msg: StreamMessage) => {
        if (msg.type === "snapshot.anatomy" || msg.type === "anatomy.patch") {
          setSnapshot(msg.payload as AnatomySnapshot);
        }
      },
    });
    stream.start();
    return () => stream.stop();
  }, [id, initial.data]);

  // Pause toggle: gate the stream so the canvas freezes for study.
  useEffect(() => {
    // The current canvas always renders whatever `snapshot` holds — we
    // don't need to do anything explicit on pause beyond stopping
    // setSnapshot. This effect is here to wire a future buffered-update
    // counter once anatomy.patch carries deltas instead of full snapshots.
  }, [paused]);

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4">
      <Link
        href={{ pathname: `/darwins/${id}` }}
        className="inline-flex items-center gap-1 text-xs text-[var(--color-muted-foreground)] hover:underline"
      >
        <ArrowLeft className="size-3" /> Back to Darwin
      </Link>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Anatomy</CardTitle>
            <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
              Orchestrator + active neurons · {snapshot?.neurons.length ?? "—"} nodes ·{" "}
              <ConnectionPill state={connection} />
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPaused((p) => !p)}
              title={paused ? "Resume live updates" : "Pause live updates"}
            >
              {paused ? <Play className="mr-1 size-4" /> : <Pause className="mr-1 size-4" />}
              {paused ? "Resume" : "Pause"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void initial.refetch()}
              title="Refetch snapshot"
            >
              <RefreshCw className="mr-1 size-4" />
              Refresh
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {!snapshot ? (
            <Skeleton className="h-96 w-full" />
          ) : (
            <div className="grid gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,1fr)]">
              <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-background)] p-2">
                <AnatomyCanvas
                  snapshot={snapshot}
                  selectedNeuronId={selectedNeuronId}
                  onSelectNeuron={setSelectedNeuronId}
                />
              </div>
              <SidePanel
                snapshot={snapshot}
                selectedNeuronId={selectedNeuronId}
                onClear={() => setSelectedNeuronId(null)}
              />
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function ConnectionPill({ state }: { state: ConnectionState }) {
  const color =
    state === "open"
      ? "text-[var(--color-status-alive)]"
      : state === "connecting"
        ? "text-[var(--color-alert-warning)]"
        : "text-[var(--color-muted-foreground)]";
  const label = state === "open" ? "live" : state === "connecting" ? "connecting" : "offline";
  return <span className={color}>{label}</span>;
}

function SidePanel({
  snapshot,
  selectedNeuronId,
  onClear,
}: {
  snapshot: AnatomySnapshot;
  selectedNeuronId: string | null;
  onClear: () => void;
}) {
  if (selectedNeuronId) {
    const n = snapshot.neurons.find((x) => x.id === selectedNeuronId);
    if (!n) {
      return null;
    }
    return (
      <div className="space-y-3 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-3 text-sm">
        <div className="flex items-start justify-between">
          <div>
            <div className="mono font-semibold">{n.id}</div>
            <div className="text-xs text-[var(--color-muted-foreground)]">{n.category}</div>
          </div>
          <button
            type="button"
            onClick={onClear}
            className="text-xs text-[var(--color-muted-foreground)] hover:underline"
          >
            Clear
          </button>
        </div>
        <dl className="grid grid-cols-2 gap-y-1 text-xs">
          <dt className="text-[var(--color-muted-foreground)]">Status</dt>
          <dd className="capitalize">{n.status}</dd>
          <dt className="text-[var(--color-muted-foreground)]">Recent dispatches</dt>
          <dd>{n.dispatch_count_recent}</dd>
          <dt className="text-[var(--color-muted-foreground)]">Last dispatch</dt>
          <dd>{n.last_dispatch_at ? formatRelative(n.last_dispatch_at) : "—"}</dd>
          <dt className="text-[var(--color-muted-foreground)]">Last error</dt>
          <dd>{n.last_error_at ? formatRelative(n.last_error_at) : "—"}</dd>
        </dl>
      </div>
    );
  }

  // Default panel: orchestrator state + DNA summary
  const heatTop = Object.entries(snapshot.heatmap)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5);
  return (
    <div className="space-y-3 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-3 text-sm">
      <div>
        <div className="text-xs uppercase tracking-wide text-[var(--color-muted-foreground)]">
          Orchestrator
        </div>
        <div className="capitalize">{snapshot.orchestrator.state}</div>
        {snapshot.orchestrator.tick !== null && (
          <div className="text-xs text-[var(--color-muted-foreground)]">
            tick {snapshot.orchestrator.tick}
          </div>
        )}
      </div>
      <div>
        <div className="text-xs uppercase tracking-wide text-[var(--color-muted-foreground)]">
          Top dispatchers
        </div>
        <ul className="mt-1 space-y-0.5 text-xs">
          {heatTop.length === 0 && (
            <li className="text-[var(--color-muted-foreground)]">No recent activity.</li>
          )}
          {heatTop.map(([id, w]) => (
            <li key={id} className="flex items-center justify-between">
              <span className="mono truncate">{id}</span>
              <span className="text-[var(--color-muted-foreground)]">
                {Math.round(w * 100)}%
              </span>
            </li>
          ))}
        </ul>
      </div>
      <div className="text-[10px] text-[var(--color-muted-foreground)]">
        Click a node to inspect.
      </div>
    </div>
  );
}
