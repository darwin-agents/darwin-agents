"use client";

import Link from "next/link";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useQuery } from "@tanstack/react-query";
import { Pause, Play } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/darwin/status-badge";
import { AlertBadges } from "@/components/darwin/alert-badges";
import { KillDialog } from "@/components/darwin/kill-dialog";
import { shortId } from "@/lib/format";
import { lifecycleOf } from "@/lib/types";

export function DarwinHeader({ darwinId }: { darwinId: string }) {
  const queryClient = useQueryClient();

  const { data: status } = useQuery({
    queryKey: queryKeys.darwin(darwinId),
    queryFn: () => api.getDarwin(darwinId),
    refetchInterval: 10_000,
  });
  const { data: alerts } = useQuery({
    queryKey: queryKeys.alerts(darwinId),
    queryFn: () => api.alerts(darwinId),
    refetchInterval: 30_000,
  });

  const lifecycle = lifecycleOf(status);
  const terminal = lifecycle === "dead" || lifecycle === "killed";
  // Resume un-pauses, and restarts a crashed drive loop.
  const paused = lifecycle === "paused" || lifecycle === "crashed";

  const pause = useMutation({
    mutationFn: () => api.pause(darwinId),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.darwin(darwinId) }),
  });
  const resume = useMutation({
    mutationFn: () => api.resume(darwinId),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.darwin(darwinId) }),
  });

  return (
    <header className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--color-border)] pb-4">
      <div className="min-w-0 space-y-1">
        <div className="flex items-center gap-2 text-xs text-[var(--color-muted-foreground)]">
          <Link href={{ pathname: "/" }} className="hover:underline">
            Fleet
          </Link>
          <span>/</span>
          <span>Darwin</span>
        </div>
        <div className="flex items-center gap-3">
          <h1 className="mono text-xl font-semibold tracking-tight">{shortId(darwinId, 12, 6)}</h1>
          <StatusBadge status={lifecycle} />
          <AlertBadges alerts={alerts?.alerts} />
        </div>
      </div>
      <div className="flex items-center gap-2">
        {!terminal && !paused && (
          <Button
            variant="outline"
            size="sm"
            disabled={pause.isPending}
            onClick={() => pause.mutate()}
          >
            <Pause className="mr-1 size-4" />
            {pause.isPending ? "Pausing…" : "Pause"}
          </Button>
        )}
        {paused && (
          <Button
            variant="outline"
            size="sm"
            disabled={resume.isPending}
            onClick={() => resume.mutate()}
          >
            <Play className="mr-1 size-4" />
            {resume.isPending ? "Resuming…" : "Resume"}
          </Button>
        )}
        {!terminal && <KillDialog darwinId={darwinId} />}
        {(pause.isError || resume.isError) && (
          <span className="text-xs text-[var(--color-destructive)]">
            {(pause.error instanceof ApiError && pause.error.code) ||
              (resume.error instanceof ApiError && resume.error.code) ||
              "lifecycle error"}
          </span>
        )}
      </div>
    </header>
  );
}
