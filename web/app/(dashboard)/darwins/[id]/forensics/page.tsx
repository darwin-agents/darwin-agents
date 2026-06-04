"use client";

/**
 * Post-mortem soma forensics (doc 05). Operator surface over:
 *   GET /platform/darwins/{id}/forensics                 ← snapshot index
 *   GET /platform/darwins/{id}/forensics/{snapshot_id}   ← full manifest
 *
 * When a Darwin dies the supervisor captures its whole soma tree (in-process)
 * or pulls the pod's signed layer fingerprint (k8s) into a death-time
 * snapshot, so an operator can read exactly what code was running at death.
 * This page lists those snapshots and renders one manifest — file contents for
 * an in-process capture, the layer fingerprint for a k8s one.
 *
 * Operator-scoped (PLATFORM_OPERATOR); an owner-only token gets 403 and an
 * explanatory empty state. The selected snapshot persists in `?snapshot=`.
 */
import Link from "next/link";
import { use, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { ApiError, api } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatTimestamp, shortId } from "@/lib/format";
import type { ForensicManifest, ForensicSnapshotSummary } from "@/lib/types";

export default function ForensicsPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  const pathname = usePathname();
  const sp = useSearchParams();
  const selected = sp.get("snapshot");

  const select = (snapshotId: string | null) => {
    const qs = new URLSearchParams(sp.toString());
    if (snapshotId) qs.set("snapshot", snapshotId);
    else qs.delete("snapshot");
    router.replace(`${pathname}?${qs.toString()}` as never);
  };

  const indexQ = useQuery({
    queryKey: queryKeys.forensics(id),
    queryFn: () => api.forensics(id),
    retry: false,
  });

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4">
      <Link
        href={{ pathname: `/darwins/${id}` }}
        className="inline-flex items-center gap-1 text-xs text-[var(--color-muted-foreground)] hover:underline"
      >
        <ArrowLeft className="size-3" /> Back to Darwin
      </Link>

      <Card>
        <CardHeader>
          <CardTitle>Death-time forensics</CardTitle>
          <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
            Soma snapshots captured when {id} died. Operator-scoped, read-only.
          </p>
        </CardHeader>
        <CardContent>
          {indexQ.isLoading ? (
            <Skeleton className="h-32 w-full" />
          ) : indexQ.isError ? (
            <ForensicError error={indexQ.error} />
          ) : (indexQ.data?.snapshots.length ?? 0) === 0 ? (
            <Empty>
              No forensic snapshots for this Darwin. They&apos;re captured on
              death when the supervisor runs with a configured soma root; a
              living Darwin has none yet.
            </Empty>
          ) : (
            <SnapshotIndex
              snapshots={indexQ.data?.snapshots ?? []}
              selected={selected}
              onSelect={select}
            />
          )}
        </CardContent>
      </Card>

      {selected ? <ManifestCard id={id} snapshotId={selected} /> : null}
    </div>
  );
}

function SnapshotIndex({
  snapshots,
  selected,
  onSelect,
}: {
  snapshots: ForensicSnapshotSummary[];
  selected: string | null;
  onSelect: (snapshotId: string) => void;
}) {
  return (
    <div className="overflow-auto rounded-md border border-[var(--color-border)]">
      <table className="w-full text-xs">
        <thead className="bg-[var(--color-muted)]/40">
          <tr>
            {["snapshot", "captured", "reason", "files", "soma_digest"].map(
              (c) => (
                <th
                  key={c}
                  className="mono px-3 py-1.5 text-left font-normal text-[var(--color-muted-foreground)]"
                >
                  {c}
                </th>
              ),
            )}
          </tr>
        </thead>
        <tbody>
          {snapshots.map((s) => {
            const active = s.snapshot_id === selected;
            return (
              <tr
                key={s.snapshot_id}
                onClick={() => onSelect(s.snapshot_id)}
                className={
                  active
                    ? "cursor-pointer border-t border-[var(--color-border)] bg-[var(--color-muted)]/40"
                    : "cursor-pointer border-t border-[var(--color-border)] hover:bg-[var(--color-muted)]/30"
                }
              >
                <td className="mono px-3 py-1.5">{s.snapshot_id}</td>
                <td className="px-3 py-1.5">
                  {formatTimestamp(s.captured_at ?? undefined)}
                </td>
                <td className="px-3 py-1.5">{s.reason ?? "—"}</td>
                <td className="mono px-3 py-1.5">{s.file_count ?? "—"}</td>
                <td className="mono px-3 py-1.5">
                  {s.soma_digest ? shortId(s.soma_digest, 10, 6) : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ManifestCard({ id, snapshotId }: { id: string; snapshotId: string }) {
  const q = useQuery({
    queryKey: queryKeys.forensicSnapshot(id, snapshotId),
    queryFn: () => api.forensicSnapshot(id, snapshotId),
    retry: false,
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="mono text-sm">{snapshotId}</CardTitle>
      </CardHeader>
      <CardContent>
        {q.isLoading ? (
          <Skeleton className="h-48 w-full" />
        ) : q.isError ? (
          <ForensicError error={q.error} />
        ) : q.data ? (
          <Manifest manifest={q.data} />
        ) : (
          <Empty>No manifest.</Empty>
        )}
      </CardContent>
    </Card>
  );
}

function Manifest({ manifest }: { manifest: ForensicManifest }) {
  const meta: Array<[string, unknown]> = [
    ["schema", manifest.schema],
    ["reason", manifest.reason],
    ["captured_at", formatTimestamp(manifest.captured_at)],
    ["file_count", manifest.file_count],
    ["total_bytes", manifest.total_bytes],
    ["soma_digest", manifest.soma_digest],
    ["mutation_snapshot_ids", manifest.mutation_snapshot_ids?.join(", ")],
  ];
  const contents = manifest.contents ?? {};
  const paths = Object.keys(contents).sort();
  const layers = manifest.layers ?? [];

  return (
    <div className="space-y-4">
      <div className="overflow-auto rounded-md border border-[var(--color-border)]">
        <table className="w-full text-xs">
          <tbody>
            {meta
              .filter(([, v]) => v !== undefined && v !== null && v !== "")
              .map(([k, v]) => (
                <tr
                  key={k}
                  className="border-b border-[var(--color-border)] last:border-b-0"
                >
                  <td className="mono px-3 py-1.5 text-[var(--color-muted-foreground)]">
                    {k}
                  </td>
                  <td className="mono break-all px-3 py-1.5">{String(v)}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>

      {/* k8s death: signed layer fingerprint, no file bodies. */}
      {layers.length > 0 && (
        <div>
          <h3 className="mb-1 text-xs font-medium text-[var(--color-muted-foreground)]">
            Layers ({layers.length}) — k8s code fingerprint
          </h3>
          <pre className="mono max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-md border border-[var(--color-border)] px-3 py-2 text-[11px]">
            {JSON.stringify(layers, null, 2)}
          </pre>
        </div>
      )}

      {/* In-process death: the captured soma file tree. */}
      {paths.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-xs font-medium text-[var(--color-muted-foreground)]">
            Soma files ({paths.length})
          </h3>
          {paths.map((p) => (
            <FileBlock key={p} path={p} body={contents[p] ?? null} />
          ))}
        </div>
      )}
    </div>
  );
}

function FileBlock({ path, body }: { path: string; body: string | null }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-md border border-[var(--color-border)]">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="mono flex w-full items-center justify-between px-3 py-1.5 text-left text-xs hover:bg-[var(--color-muted)]/30"
      >
        <span className="break-all">{path}</span>
        <span className="text-[var(--color-muted-foreground)]">
          {body === null ? "binary" : open ? "−" : "+"}
        </span>
      </button>
      {open && body !== null && (
        <pre className="mono max-h-96 overflow-auto whitespace-pre-wrap break-all border-t border-[var(--color-border)] px-3 py-2 text-[11px]">
          {body}
        </pre>
      )}
    </div>
  );
}

function ForensicError({ error }: { error: unknown }) {
  if (error instanceof ApiError && error.isForbidden) {
    return (
      <Empty>
        Forensics are operator-scoped. Your token lacks the platform-operator
        role.
      </Empty>
    );
  }
  if (error instanceof ApiError && error.isNotFound) {
    return <Empty>That snapshot no longer exists for this Darwin.</Empty>;
  }
  const msg =
    error instanceof ApiError
      ? `Platform error ${error.status}: ${error.code}`
      : error instanceof Error
        ? error.message
        : "Unknown error";
  return <Empty>Couldn&apos;t load forensics. {msg}</Empty>;
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-[var(--color-border)] p-6 text-center text-xs text-[var(--color-muted-foreground)]">
      {children}
    </div>
  );
}
