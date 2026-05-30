"use client";

/**
 * Data browser (M3-E) — low-level read-only inspector. Four tabs over the
 * existing platform read-endpoints:
 *   Lifecycle   ← GET /darwins/{id}
 *   Lineage     ← GET /darwins/{id}/lineage/tree
 *   Ledger      ← GET /darwins/{id}/ledger
 *   Audit chain ← GET /darwins/{id}/events
 *
 * Goal: surface the raw shape, not a polished view. The Darwin shell, Billing,
 * Lineage, and Timeline pages already do the polished story; this page is for
 * "what's actually in the row".
 *
 * Active tab persists in `?tab=…` so a link is shareable.
 */
import Link from "next/link";
import { use, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type {
  DarwinEvent,
  LedgerEntry,
  LineageNode,
} from "@/lib/types";

const TABS = ["lifecycle", "lineage", "ledger", "audit"] as const;
type Tab = (typeof TABS)[number];

export default function DataPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  const pathname = usePathname();
  const sp = useSearchParams();
  const tab: Tab = (TABS.find((t) => t === sp.get("tab")) ?? "lifecycle") as Tab;

  const setTab = (next: Tab) => {
    const qs = new URLSearchParams(sp.toString());
    qs.set("tab", next);
    router.replace(`${pathname}?${qs.toString()}` as never);
  };

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4">
      <Link
        href={{ pathname: `/darwins/${id}` }}
        className="inline-flex items-center gap-1 text-xs text-[var(--color-muted-foreground)] hover:underline"
      >
        <ArrowLeft className="size-3" /> Back to Darwin
      </Link>

      <Card>
        <CardHeader>
          <CardTitle>Data browser</CardTitle>
          <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
            Raw row inspector for {id}. Read-only.
          </p>
        </CardHeader>
        <CardContent className="p-0">
          <div className="flex border-b border-[var(--color-border)]">
            {TABS.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTab(t)}
                className={
                  t === tab
                    ? "border-b-2 border-[var(--color-accent)] px-4 py-2 text-xs font-medium text-[var(--color-foreground)]"
                    : "border-b-2 border-transparent px-4 py-2 text-xs text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]"
                }
              >
                {t}
              </button>
            ))}
          </div>
          <div className="p-4">
            {tab === "lifecycle" && <LifecycleTab id={id} />}
            {tab === "lineage" && <LineageTab id={id} />}
            {tab === "ledger" && <LedgerTab id={id} />}
            {tab === "audit" && <AuditTab id={id} />}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

/* ---------------------------------------------------------------- lifecycle */

function LifecycleTab({ id }: { id: string }) {
  const q = useQuery({
    queryKey: queryKeys.darwin(id),
    queryFn: () => api.getDarwin(id),
    refetchInterval: 10_000,
  });
  if (q.isLoading) return <Skeleton className="h-48 w-full" />;
  if (!q.data) return <Empty>No lifecycle row.</Empty>;
  return <KeyValueTable rows={Object.entries(q.data)} />;
}

/* ------------------------------------------------------------------ lineage */

function LineageTab({ id }: { id: string }) {
  const q = useQuery({
    queryKey: queryKeys.lineage(id),
    queryFn: () => api.lineageTree(id),
  });
  if (q.isLoading) return <Skeleton className="h-48 w-full" />;
  if (!q.data) return <Empty>No lineage data.</Empty>;

  const flat: Array<{ relation: string; node: LineageNode }> = [];
  if (q.data.root) flat.push({ relation: "self", node: q.data.root });
  for (const a of q.data.ancestors ?? []) flat.push({ relation: "ancestor", node: a });
  walkDescendants(q.data.descendants ?? q.data.root?.children ?? [], "descendant", flat);

  if (flat.length === 0) return <Empty>No lineage edges.</Empty>;

  return (
    <Table
      cols={["relation", "darwin_id", "generation", "parent_id", "fitness", "lifecycle"]}
      rows={flat.map(({ relation, node }) => [
        relation,
        node.darwin_id,
        node.generation ?? "—",
        node.parent_id ?? "—",
        node.fitness ?? "—",
        node.lifecycle ?? "—",
      ])}
    />
  );
}

function walkDescendants(
  nodes: LineageNode[],
  relation: string,
  out: Array<{ relation: string; node: LineageNode }>,
) {
  for (const n of nodes) {
    out.push({ relation, node: n });
    if (n.children?.length) walkDescendants(n.children, "descendant", out);
  }
}

/* ------------------------------------------------------------------- ledger */

function LedgerTab({ id }: { id: string }) {
  const sp = useSearchParams();
  const offset = Number(sp.get("ledger_offset") ?? 0);
  const router = useRouter();
  const pathname = usePathname();

  const q = useQuery({
    queryKey: queryKeys.ledger(id, offset),
    queryFn: () => api.ledger(id, { limit: 100, offset }),
  });

  const setOffset = (next: number) => {
    const qs = new URLSearchParams(sp.toString());
    if (next > 0) qs.set("ledger_offset", String(next));
    else qs.delete("ledger_offset");
    router.replace(`${pathname}?${qs.toString()}` as never);
  };

  if (q.isLoading) return <Skeleton className="h-48 w-full" />;
  const entries = q.data?.entries ?? [];
  const total = q.data?.total ?? 0;
  return (
    <div className="space-y-2">
      <Table
        cols={["seq", "ts", "type", "direction", "amount_usdc", "counterparty", "category", "memo"]}
        rows={entries.map((e: LedgerEntry) => [
          e.seq,
          e.ts,
          e.type,
          e.direction,
          e.amount_usdc,
          e.counterparty ?? "—",
          e.category ?? "—",
          e.memo ?? "—",
        ])}
      />
      <Pager
        offset={offset}
        pageSize={100}
        total={total}
        onChange={setOffset}
        label={`${offset + 1}–${offset + entries.length} of ${total}`}
      />
    </div>
  );
}

/* -------------------------------------------------------------------- audit */

function AuditTab({ id }: { id: string }) {
  const sp = useSearchParams();
  const offset = Number(sp.get("audit_offset") ?? 0);
  const router = useRouter();
  const pathname = usePathname();

  const q = useQuery({
    queryKey: queryKeys.events(id, offset, { source: "data-browser" }),
    queryFn: () => api.events(id, { limit: 100, offset }),
  });

  const setOffset = (next: number) => {
    const qs = new URLSearchParams(sp.toString());
    if (next > 0) qs.set("audit_offset", String(next));
    else qs.delete("audit_offset");
    router.replace(`${pathname}?${qs.toString()}` as never);
  };

  if (q.isLoading) return <Skeleton className="h-48 w-full" />;
  const events = q.data?.events ?? [];
  const total = q.data?.total ?? 0;
  return (
    <div className="space-y-2">
      <Table
        cols={["seq", "ts", "kind", "neuron_id", "cost_usdc", "hash"]}
        rows={events.map((e: DarwinEvent) => [
          e.seq ?? "—",
          String(e.ts),
          e.kind ?? "—",
          e.neuron_id ?? "—",
          e.cost_usdc ?? "—",
          e.hash ? String(e.hash).slice(0, 16) + "…" : "—",
        ])}
        rawRows={events as unknown as Array<Record<string, unknown>>}
      />
      <Pager
        offset={offset}
        pageSize={100}
        total={total}
        onChange={setOffset}
        label={`${offset + 1}–${offset + events.length} of ${total}`}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ helpers */

function KeyValueTable({ rows }: { rows: Array<[string, unknown]> }) {
  return (
    <div className="overflow-auto rounded-md border border-[var(--color-border)]">
      <table className="w-full text-xs">
        <tbody>
          {rows.map(([k, v]) => (
            <tr key={k} className="border-b border-[var(--color-border)] last:border-b-0">
              <td className="mono px-3 py-1.5 text-[var(--color-muted-foreground)]">{k}</td>
              <td className="mono break-all px-3 py-1.5">
                {typeof v === "object" ? JSON.stringify(v) : String(v)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Table({
  cols,
  rows,
  rawRows,
}: {
  cols: string[];
  rows: Array<Array<string | number | null | undefined>>;
  /** Optional per-row raw JSON for an expand-on-click drawer. */
  rawRows?: Array<Record<string, unknown>>;
}) {
  return (
    <div className="overflow-auto rounded-md border border-[var(--color-border)]">
      <table className="w-full text-xs">
        <thead className="bg-[var(--color-muted)]/40">
          <tr>
            {cols.map((c) => (
              <th
                key={c}
                className="mono px-3 py-1.5 text-left font-normal text-[var(--color-muted-foreground)]"
              >
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={cols.length} className="px-3 py-6 text-center text-[var(--color-muted-foreground)]">
                No rows.
              </td>
            </tr>
          )}
          {rows.map((r, i) => (
            <ExpandableRow key={i} cells={r} raw={rawRows?.[i]} cols={cols.length} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ExpandableRow({
  cells,
  raw,
  cols,
}: {
  cells: Array<string | number | null | undefined>;
  raw?: Record<string, unknown>;
  cols: number;
}) {
  const [open, setOpen] = useState(false);
  const expandable = raw !== undefined;
  return (
    <>
      <tr
        className={
          expandable
            ? "border-b border-[var(--color-border)] last:border-b-0 hover:bg-[var(--color-muted)]/30"
            : "border-b border-[var(--color-border)] last:border-b-0"
        }
        onClick={() => expandable && setOpen((o) => !o)}
        style={{ cursor: expandable ? "pointer" : undefined }}
      >
        {cells.map((c, i) => (
          <td key={i} className="mono break-all px-3 py-1.5">
            {c === null || c === undefined ? "—" : String(c)}
          </td>
        ))}
      </tr>
      {open && raw && (
        <tr>
          <td colSpan={cols} className="bg-[var(--color-background)] p-0">
            <pre className="mono max-h-72 overflow-auto whitespace-pre-wrap break-all px-3 py-2 text-[11px]">
              {JSON.stringify(raw, null, 2)}
            </pre>
          </td>
        </tr>
      )}
    </>
  );
}

function Pager({
  offset,
  pageSize,
  total,
  onChange,
  label,
}: {
  offset: number;
  pageSize: number;
  total: number;
  onChange: (next: number) => void;
  label: string;
}) {
  const hasPrev = offset > 0;
  const hasNext = offset + pageSize < total;
  return (
    <div className="flex items-center justify-between text-xs text-[var(--color-muted-foreground)]">
      <span>{label}</span>
      <div className="flex gap-1">
        <button
          type="button"
          disabled={!hasPrev}
          onClick={() => onChange(Math.max(0, offset - pageSize))}
          className="rounded-md border border-[var(--color-border)] px-2 py-1 disabled:opacity-50 enabled:hover:bg-[var(--color-muted)]"
        >
          ← Prev
        </button>
        <button
          type="button"
          disabled={!hasNext}
          onClick={() => onChange(offset + pageSize)}
          className="rounded-md border border-[var(--color-border)] px-2 py-1 disabled:opacity-50 enabled:hover:bg-[var(--color-muted)]"
        >
          Next →
        </button>
      </div>
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-[var(--color-border)] p-6 text-center text-xs text-[var(--color-muted-foreground)]">
      {children}
    </div>
  );
}
