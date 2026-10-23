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
import type { LineageNode } from "@/lib/types";

export default function LineagePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data, isLoading, isError, error } = useQuery({
    queryKey: queryKeys.lineage(id),
    queryFn: () => api.lineageTree(id),
  });

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
          {data && (
            <div className="space-y-1">
              {[...data.ancestors].reverse().map((a, i) => (
                <Row key={a.darwin_id} id={a.darwin_id} alive={a.alive} depth={i} note="ancestor" />
              ))}
              <Row
                id={data.darwin_id}
                alive
                depth={data.ancestors.length}
                note={`gen ${data.generation}`}
                me
              />
              {data.descendants.map((child) => (
                <TreeNode key={child.darwin_id} node={child} depth={data.ancestors.length + 1} />
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function Row({
  id,
  alive,
  depth,
  note,
  me = false,
}: {
  id: string;
  alive: boolean;
  depth: number;
  note?: string;
  me?: boolean;
}) {
  return (
    <div
      className="flex items-center gap-2 rounded px-2 py-1 text-sm"
      style={{ paddingLeft: `${depth * 16 + 8}px` }}
    >
      <Link
        href={{ pathname: `/darwins/${id}` }}
        className={`mono ${me ? "font-bold text-[var(--color-accent)]" : "hover:underline"}`}
      >
        {shortId(id, 10, 6)}
      </Link>
      <StatusBadge status={alive ? "alive" : "dead"} />
      {note && <span className="text-xs text-[var(--color-muted-foreground)]">{note}</span>}
    </div>
  );
}

function TreeNode({ node, depth }: { node: LineageNode; depth: number }) {
  return (
    <>
      <Row
        id={node.darwin_id}
        alive={node.alive}
        depth={depth}
        note={`child · budget $${node.budget_usdc.toFixed(2)}`}
      />
      {node.children?.map((child) => (
        <TreeNode key={child.darwin_id} node={child} depth={depth + 1} />
      ))}
    </>
  );
}
