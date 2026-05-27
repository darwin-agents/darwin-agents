"use client";

import Link from "next/link";
import { use } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Server, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { formatRelative } from "@/lib/format";

interface ResourceRecord {
  id: string;
  darwin_id: string;
  kind: string;
  spec: Record<string, unknown>;
  creds_ref: string;
  status: "active" | "deprovisioned" | string;
  created_at: number;
  deprovisioned_at: number | null;
}

export default function ResourcesPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const queryClient = useQueryClient();
  const queryKey = ["darwin", id, "resources"] as const;

  const { data, isLoading } = useQuery({
    queryKey,
    queryFn: () => api.listResources(id),
  });

  const remove = useMutation({
    mutationFn: (resourceId: string) => api.deleteResource(id, resourceId),
    onSettled: () => queryClient.invalidateQueries({ queryKey }),
  });

  const resources = ((data?.resources ?? []) as unknown as ResourceRecord[]).slice().sort(
    (a, b) => b.created_at - a.created_at,
  );
  const active = resources.filter((r) => r.status === "active");

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4">
      <Link
        href={{ pathname: `/darwins/${id}` }}
        className="inline-flex items-center gap-1 text-xs text-[var(--color-muted-foreground)] hover:underline"
      >
        <ArrowLeft className="size-3" /> Back to Darwin
      </Link>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Server className="size-4 text-[var(--color-muted-foreground)]" />
            Provisioned resources
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="space-y-2 p-3">
              {Array.from({ length: 2 }).map((_, i) => (
                <Skeleton key={i} className="h-14 w-full" />
              ))}
            </div>
          ) : active.length === 0 ? (
            <p className="px-4 pb-4 text-sm text-[var(--color-muted-foreground)]">
              No active resources. Darwin hasn&apos;t provisioned any databases, buckets, or jobs yet.
            </p>
          ) : (
            <ul className="divide-y divide-[var(--color-border)]">
              {active.map((r) => (
                <li key={r.id} className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 px-4 py-3">
                  <div className="min-w-0">
                    <div className="flex items-baseline gap-2">
                      <span className="text-sm font-medium capitalize">{r.kind}</span>
                      <span className="mono text-xs text-[var(--color-muted-foreground)]">
                        {r.id}
                      </span>
                    </div>
                    <div className="mt-1 flex flex-wrap gap-x-3 text-[11px] text-[var(--color-muted-foreground)]">
                      <span>created {formatRelative(r.created_at)}</span>
                      <span className="mono">creds {shortVaultPath(r.creds_ref)}</span>
                    </div>
                    {Object.keys(r.spec).length > 0 && (
                      <details className="mt-1">
                        <summary className="cursor-pointer text-[11px] text-[var(--color-muted-foreground)]">
                          spec
                        </summary>
                        <pre className="mono mt-1 overflow-auto rounded bg-[var(--color-background)] p-2 text-[10px]">
                          {JSON.stringify(r.spec, null, 2)}
                        </pre>
                      </details>
                    )}
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Deprovision"
                    disabled={remove.isPending && remove.variables === r.id}
                    onClick={() => {
                      if (
                        window.confirm(
                          `Deprovision ${r.kind} resource ${r.id}? This is permanent — the platform releases the resource and revokes its credentials.`,
                        )
                      ) {
                        remove.mutate(r.id);
                      }
                    }}
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {remove.isError && (
        <p className="text-xs text-[var(--color-destructive)]">
          Deprovision failed: {(remove.error as Error).message}
        </p>
      )}
    </div>
  );
}

function shortVaultPath(p: string): string {
  if (!p) return "—";
  if (p.length <= 32) return p;
  return `${p.slice(0, 16)}…${p.slice(-12)}`;
}
