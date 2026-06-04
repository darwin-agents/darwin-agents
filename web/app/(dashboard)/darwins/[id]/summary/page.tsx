"use client";

/**
 * Accounting summary (docs 05, 17) ← GET /darwins/{id}/ledger/summary?year=
 *
 * A jurisdiction-neutral roll-up of a Darwin's ledger: totals in/out/net plus a
 * per-category breakdown, for a tax year (UTC) or lifetime. This is NOT a
 * statutory tax form — the platform does not file taxes. The owner hands this,
 * or the raw ledger CSV, to their accountant. Owner-scoped.
 *
 * The selected year persists in `?year=` so a link is shareable; "Lifetime"
 * drops the param.
 */
import Link from "next/link";
import { use } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Download } from "lucide-react";
import { ApiError, api, buildApiUrl } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatSignedUsdc, formatUsdc } from "@/lib/format";
import type { LedgerSummary } from "@/lib/types";

// Span of selectable tax years: the current UTC year back a few, plus Lifetime.
const THIS_YEAR = new Date().getUTCFullYear();
const YEARS = [THIS_YEAR, THIS_YEAR - 1, THIS_YEAR - 2, THIS_YEAR - 3];

export default function LedgerSummaryPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  const pathname = usePathname();
  const sp = useSearchParams();
  const yearParam = sp.get("year");
  const year = yearParam ? Number(yearParam) : undefined;

  const setYear = (next: number | undefined) => {
    const qs = new URLSearchParams(sp.toString());
    if (next) qs.set("year", String(next));
    else qs.delete("year");
    router.replace(`${pathname}?${qs.toString()}` as never);
  };

  const q = useQuery({
    queryKey: queryKeys.ledgerSummary(id, year),
    queryFn: () => api.ledgerSummary(id, year),
  });

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
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <CardTitle>Accounting summary</CardTitle>
              <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
                Jurisdiction-neutral roll-up for {id}. Not a statutory tax form
                — hand this, or the{" "}
                <a
                  href={buildApiUrl(`/darwins/${id}/ledger.csv`)}
                  className="inline-flex items-center gap-1 underline hover:text-[var(--color-foreground)]"
                >
                  <Download className="size-3" /> raw ledger CSV
                </a>
                , to your accountant.
              </p>
            </div>
            <div className="flex flex-wrap gap-1">
              <YearChip
                label="Lifetime"
                active={year === undefined}
                onClick={() => setYear(undefined)}
              />
              {YEARS.map((y) => (
                <YearChip
                  key={y}
                  label={String(y)}
                  active={year === y}
                  onClick={() => setYear(y)}
                />
              ))}
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-5">
          {q.isLoading ? (
            <Skeleton className="h-56 w-full" />
          ) : q.isError ? (
            <ErrorPanel error={q.error} />
          ) : q.data ? (
            <SummaryBody summary={q.data} />
          ) : (
            <Empty>No summary.</Empty>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function SummaryBody({ summary }: { summary: LedgerSummary }) {
  const categories = Object.entries(summary.by_category ?? {}).sort(
    (a, b) => b[1].amount_usdc - a[1].amount_usdc,
  );
  return (
    <>
      <div className="grid gap-3 sm:grid-cols-3">
        <Metric label="Total in" value={formatUsdc(summary.total_in_usdc)} tone="in" />
        <Metric label="Total out" value={formatUsdc(summary.total_out_usdc)} tone="out" />
        <Metric label="Net" value={formatSignedUsdc(summary.net_usdc)} tone="net" />
      </div>

      <div className="text-xs text-[var(--color-muted-foreground)]">
        {summary.movement_count} movement{summary.movement_count === 1 ? "" : "s"}{" "}
        in {summary.currency} ·{" "}
        {summary.period.year
          ? `tax year ${summary.period.year} (UTC)`
          : "lifetime"}
        {summary.period.start_utc && summary.period.end_utc ? (
          <>
            {" "}· {summary.period.start_utc} → {summary.period.end_utc}
          </>
        ) : null}
        {summary.undated_excluded ? (
          <>
            {" "}· {summary.undated_excluded} undated movement
            {summary.undated_excluded === 1 ? "" : "s"} excluded
          </>
        ) : null}
      </div>

      <div className="overflow-auto rounded-md border border-[var(--color-border)]">
        <table className="w-full text-xs">
          <thead className="bg-[var(--color-muted)]/40">
            <tr>
              <th className="px-3 py-1.5 text-left font-normal text-[var(--color-muted-foreground)]">
                Category
              </th>
              <th className="px-3 py-1.5 text-left font-normal text-[var(--color-muted-foreground)]">
                Direction
              </th>
              <th className="px-3 py-1.5 text-right font-normal text-[var(--color-muted-foreground)]">
                Count
              </th>
              <th className="px-3 py-1.5 text-right font-normal text-[var(--color-muted-foreground)]">
                Amount
              </th>
            </tr>
          </thead>
          <tbody>
            {categories.length === 0 && (
              <tr>
                <td
                  colSpan={4}
                  className="px-3 py-6 text-center text-[var(--color-muted-foreground)]"
                >
                  No movements in this period.
                </td>
              </tr>
            )}
            {categories.map(([cat, b]) => (
              <tr
                key={cat}
                className="border-t border-[var(--color-border)]"
              >
                <td className="mono px-3 py-1.5">{cat}</td>
                <td className="px-3 py-1.5">{b.direction}</td>
                <td className="mono px-3 py-1.5 text-right">{b.count}</td>
                <td className="mono px-3 py-1.5 text-right">
                  {formatUsdc(b.amount_usdc)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function Metric({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "in" | "out" | "net";
}) {
  const color =
    tone === "in"
      ? "text-[var(--color-status-alive)]"
      : tone === "out"
        ? "text-[var(--color-alert-warning)]"
        : "text-[var(--color-foreground)]";
  return (
    <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-3">
      <div className="text-xs text-[var(--color-muted-foreground)]">{label}</div>
      <div className={`mono mt-1 text-lg ${color}`}>{value}</div>
    </div>
  );
}

function YearChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        active
          ? "rounded-md border border-[var(--color-accent)] bg-[var(--color-muted)] px-2.5 py-1 text-xs text-[var(--color-foreground)]"
          : "rounded-md border border-[var(--color-border)] px-2.5 py-1 text-xs text-[var(--color-muted-foreground)] hover:bg-[var(--color-muted)] hover:text-[var(--color-foreground)]"
      }
    >
      {label}
    </button>
  );
}

function ErrorPanel({ error }: { error: unknown }) {
  const msg =
    error instanceof ApiError
      ? error.isNotFound
        ? "This Darwin has no ledger (unknown id)."
        : `Platform error ${error.status}: ${error.code}`
      : error instanceof Error
        ? error.message
        : "Unknown error";
  return (
    <div className="rounded-md border border-[var(--color-destructive)]/40 bg-[var(--color-destructive)]/10 p-4 text-sm">
      <div className="font-medium">Couldn&apos;t load the summary.</div>
      <div className="mt-1">{msg}</div>
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
