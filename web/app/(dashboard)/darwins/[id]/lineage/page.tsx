"use client";

import Link from "next/link";
import { use } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusBadge } from "@/components/darwin/status-badge";
import { Skeleton } from "@/components/ui/skeleton";
import { shortId } from "@/lib/format";
import type { LineageNode, Lifecycle } from "@/lib/types";

export default function LineagePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data, isLoading, isError, error } = useQuery({
    queryKey: queryKeys.lineage(id),
    queryFn: () => api.lineageTree(id),
  });

  // Tolerate either { root, ancestors, descendants } or a flat tree response
  // — the runtime currently returns the latter shape.
  const root: LineageNode | undefined =
    (data?.root as LineageNode | undefined) ??
    (data as unknown as LineageNode | undefined);

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4">
      <Link
        href={{ pathname: `/darwins/${id}` }}
        className="inline-flex items-center gap-1 text-xs text-[var(--color-muted-foreground)] hover:underline"
      >
        <ArrowLeft className="size-3" /> Back to Darwin
      </Link>
      <Card>
        <CardHeader>
          <CardTitle>Lineage</CardTitle>
        </CardHeader>
        <CardContent>
          {isLoading && <Skeleton className="h-32 w-full" />}
          {isError && (
            <p className="text-sm text-[var(--color-destructive)]">
              Failed to load lineage: {(error as Error).message}
            </p>
          )}
          {root && <TreeNode node={root} highlight={id} depth={0} />}
        </CardContent>
      </Card>
    </div>
  );
}

function TreeNode({
  node,
  highlight,
  depth,
}: {
  node: LineageNode;
  highlight: string;
  depth: number;
}) {
  const isMe = node.darwin_id === highlight;
  const lifecycle = (node.lifecycle ?? "alive") as Lifecycle;
  return (
    <div className="space-y-1">
      <div
        className="flex items-center gap-2 rounded px-2 py-1 text-sm"
        style={{ paddingLeft: `${depth * 16 + 8}px` }}
      >
        <Link
          href={{ pathname: `/darwins/${node.darwin_id}` }}
          className={`mono ${isMe ? "font-bold text-[var(--color-accent)]" : "hover:underline"}`}
        >
          {shortId(node.darwin_id, 10, 6)}
        </Link>
        <StatusBadge status={lifecycle} />
        {node.generation !== undefined && (
          <span className="text-xs text-[var(--color-muted-foreground)]">
            gen {node.generation}
          </span>
        )}
        {node.cause_of_death && (
          <span className="text-xs text-[var(--color-muted-foreground)]">
            · {node.cause_of_death}
          </span>
        )}
      </div>
      {node.children?.map((child) => (
        <TreeNode
          key={child.darwin_id}
          node={child}
          highlight={highlight}
          depth={depth + 1}
        />
      ))}
    </div>
  );
}
