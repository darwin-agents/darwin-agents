"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { FleetRow } from "@/components/darwin/fleet-row";

export default function FleetPage() {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: queryKeys.darwins(),
    queryFn: () => api.listDarwins(),
    refetchInterval: 30_000,
  });

  const ids = data?.darwins ?? [];

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Fleet</h1>
          <p className="mt-1 text-sm text-[var(--color-muted-foreground)]">
            {isLoading ? "Loading…" : `${ids.length} Darwin${ids.length === 1 ? "" : "s"}`}
          </p>
        </div>
        <Button asChild>
          <Link href={{ pathname: "/darwins/new" }} className="inline-flex items-center gap-2">
            <Plus className="size-4" />
            Spawn Darwin
          </Link>
        </Button>
      </header>

      {isError && (
        <ErrorPanel
          error={error}
          onRetry={() => {
            void refetch();
          }}
        />
      )}

      {isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-16 w-full" />
          ))}
        </div>
      ) : ids.length === 0 ? (
        <EmptyState />
      ) : (
        <div className="space-y-2">
          {ids.map((id) => (
            <FleetRow key={id} darwinId={id} />
          ))}
        </div>
      )}
    </div>
  );
}

function ErrorPanel({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const msg =
    error instanceof ApiError
      ? `Platform error ${error.status}: ${error.code}`
      : error instanceof Error
        ? error.message
        : "Unknown error";
  return (
    <div className="rounded-md border border-[var(--color-destructive)]/40 bg-[var(--color-destructive)]/10 p-4 text-sm text-[var(--color-destructive)]">
      <div className="font-medium">Couldn&apos;t load fleet.</div>
      <div className="mt-1 opacity-90">{msg}</div>
      <Button onClick={onRetry} variant="outline" size="sm" className="mt-3">
        Retry
      </Button>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center gap-4 rounded-lg border border-dashed border-[var(--color-border)] py-16 text-center">
      <h2 className="text-lg font-semibold">No Darwins yet</h2>
      <p className="max-w-sm text-sm text-[var(--color-muted-foreground)]">
        A Darwin is an autonomous economic organism with its own wallet and DNA. Spawning one
        funds an opening treasury that can&apos;t be recovered if it dies.
      </p>
      <Button asChild>
        <Link href={{ pathname: "/darwins/new" }}>
          <Plus className="mr-2 size-4" /> Spawn your first Darwin
        </Link>
      </Button>
    </div>
  );
}
