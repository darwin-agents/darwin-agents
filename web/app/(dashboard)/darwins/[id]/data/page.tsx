"use client";

/**
 * Data browser (M3-E) — low-level read-only inspector. Tabs over the
 * existing platform read-endpoints:
 *   Lifecycle   ← GET /darwins/{id}
 *   Lineage     ← GET /darwins/{id}/lineage/tree
 *   Ledger      ← GET /darwins/{id}/ledger
 *   Audit chain ← GET /darwins/{id}/events
 *   Organism    ← GET /platform/darwins/{id}/organism/tables[/{t}/rows]
 *
 * Goal: surface the raw shape, not a polished view. The Darwin shell, Billing,
 * Lineage, and Timeline pages already do the polished story; this page is for
 * "what's actually in the row".
 *
 * The Organism tab reads the Darwin's *business* database through the
 * read-only dashboard role — the data the audit flagged this browser as
 * NOT showing before. It's operator-scoped (PLATFORM_OPERATOR); for an
 * owner-only token or an unprovisioned Darwin the backend returns 403/409
 * and the tab renders an explanatory empty state.
 *
 * Active tab persists in `?tab=…` so a link is shareable.
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
import type {
  DarwinEvent,
  LedgerEntry,
  LineageNode,
} from "@/lib/types";

const BASE_TABS = ["lifecycle", "lineage", "ledger", "audit"] as const;
const TABS = [...BASE_TABS, "organism"] as const;
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

  // The Organism tab reads a per-Darwin business DB that only exists in a
  // tenant-namespaced deployment, and only an operator may read it. Gate it
  // on the capability so it isn't shown where it would only ever 403/409.
  const { data: caps } = useQuery({
    queryKey: queryKeys.capabilities(),
    queryFn: () => api.capabilities(),
    staleTime: 5 * 60_000,
    retry: false,
  });
  const visibleTabs: readonly Tab[] = caps?.organism_browser
    ? TABS
    : BASE_TABS;

  const requested = sp.get("tab");
  const tab: Tab =
    (visibleTabs.find((t) => t === requested) ?? "lifecycle") as Tab;

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
            {visibleTabs.map((t) => (
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
            {tab === "organism" && <OrganismTab id={id} />}
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

/* ---------------------------------------------------------------- organism */

function OrganismTab({ id }: { id: string }) {
  const sp = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const table = sp.get("organism_table") ?? "";
  const offset = Number(sp.get("organism_offset") ?? 0);

  const tablesQ = useQuery({
    queryKey: queryKeys.organismTables(id),
    queryFn: () => api.organismTables(id),
    retry: false,
  });

  const setParam = (key: string, value: string | null) => {
    const qs = new URLSearchParams(sp.toString());
    if (value) qs.set(key, value);
    else qs.delete(key);
    router.replace(`${pathname}?${qs.toString()}` as never);
  };

  const selectTable = (next: string) => {
    const qs = new URLSearchParams(sp.toString());
    qs.set("organism_table", next);
    qs.delete("organism_offset");
    router.replace(`${pathname}?${qs.toString()}` as never);
  };

  if (tablesQ.isLoading) return <Skeleton className="h-48 w-full" />;
  if (tablesQ.isError) return <OrganismError error={tablesQ.error} />;

  const tables = tablesQ.data?.tables ?? [];
  if (tables.length === 0) {
    return (
      <Empty>
        No tables in this Darwin&apos;s organism database — it may not have
        written any business data yet.
      </Empty>
    );
  }

  const active = tables.includes(table) ? table : "";

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-[var(--color-muted-foreground)]">
          Table:
        </span>
        {tables.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => selectTable(t)}
            className={
              t === active
                ? "mono rounded-md border border-[var(--color-accent)] bg-[var(--color-muted)] px-2 py-1 text-xs text-[var(--color-foreground)]"
                : "mono rounded-md border border-[var(--color-border)] px-2 py-1 text-xs text-[var(--color-muted-foreground)] hover:bg-[var(--color-muted)] hover:text-[var(--color-foreground)]"
            }
          >
            {t}
          </button>
        ))}
      </div>
      {active ? (
        <OrganismRows
          id={id}
          table={active}
          offset={offset}
          onOffset={(next) =>
            setParam("organism_offset", next > 0 ? String(next) : null)
          }
        />
      ) : (
        <Empty>Pick a table to read a page of rows.</Empty>
      )}
    </div>
  );
}

function OrganismRows({
  id,
  table,
  offset,
  onOffset,
}: {
  id: string;
  table: string;
  offset: number;
  onOffset: (next: number) => void;
}) {
  const pageSize = 50;
  const q = useQuery({
    queryKey: queryKeys.organismRows(id, table, offset),
    queryFn: () => api.organismRows(id, table, { limit: pageSize, offset }),
    retry: false,
  });

  if (q.isLoading) return <Skeleton className="h-48 w-full" />;
  if (q.isError) return <OrganismError error={q.error} />;

  const columns = q.data?.columns ?? [];
  const rows = q.data?.rows ?? [];

  return (
    <div className="space-y-2">
      <Table
        cols={columns}
        rows={rows.map((r) =>
          columns.map((c) => {
            const v = r[c];
            return v === null || v === undefined
              ? "—"
              : typeof v === "object"
                ? JSON.stringify(v)
                : String(v);
          }),
        )}
        rawRows={rows}
      />
      <Pager
        offset={offset}
        pageSize={pageSize}
        // No total from the read-only reader; allow Next while a full page came back.
        total={offset + rows.length + (rows.length === pageSize ? pageSize : 0)}
        onChange={onOffset}
        label={`rows ${offset + 1}–${offset + rows.length}`}
      />
    </div>
  );
}

function OrganismError({ error }: { error: unknown }) {
  if (error instanceof ApiError && error.isForbidden) {
    return (
      <Empty>
        The organism data browser is operator-scoped. Your token lacks the
        platform-operator role.
      </Empty>
    );
  }
  if (error instanceof ApiError && error.status === 409) {
    return (
      <Empty>
        This Darwin has no organism database — tenant namespaces aren&apos;t
        provisioned for it (single-tenant / in-process deployment).
      </Empty>
    );
  }
  const msg =
    error instanceof ApiError
      ? `Platform error ${error.status}: ${error.code}`
      : error instanceof Error
        ? error.message
        : "Unknown error";
  return <Empty>Couldn&apos;t read organism data. {msg}</Empty>;
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
