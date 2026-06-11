"use client";

import Link from "next/link";
import { use } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Download } from "lucide-react";
import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { QueryError } from "@/components/ui/query-error";
import { TreasuryChart } from "@/components/darwin/treasury-chart";
import { FiatDialog } from "@/components/darwin/fiat-dialog";
import { formatTimestamp, formatUsdc } from "@/lib/format";

export default function BillingPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);

  const { data: billing, isError: billingError, error: billingErr } = useQuery({
    queryKey: queryKeys.billing(id),
    queryFn: () => api.billing(id),
    refetchInterval: 60_000,
  });
  const {
    data: ledger,
    isLoading,
    isError: ledgerError,
    error: ledgerErr,
  } = useQuery({
    queryKey: queryKeys.ledger(id, 0),
    queryFn: () => api.ledger(id, { limit: 500 }),
    refetchInterval: 60_000,
  });

  const entries = ledger?.entries ?? [];
  const csvUrl = api.ledgerCsvUrl(id);

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6">
      <Link
        href={{ pathname: `/darwins/${id}` }}
        className="inline-flex items-center gap-1 text-xs text-[var(--color-muted-foreground)] hover:underline"
      >
        <ArrowLeft className="size-3" /> Back to Darwin
      </Link>

      {billingError && <QueryError error={billingErr} what="billing summary" />}
      {ledgerError && <QueryError error={ledgerErr} what="the ledger" />}

      <div className="grid gap-4 sm:grid-cols-3">
        <BillingMetric label="Balance" value={formatUsdc(billing?.balance_usdc)} />
        <BillingMetric
          label="Burn / hr"
          value={billing?.burn_rate_usdc_per_hour !== undefined ? formatUsdc(-Math.abs(billing.burn_rate_usdc_per_hour)) : "—"}
        />
        <BillingMetric
          label="Ledger entries"
          value={ledger ? String(ledger.total) : "—"}
        />
      </div>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Treasury</CardTitle>
            <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
              Running balance reconstructed from the platform ledger.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <FiatDialog darwinId={id} />
            <Button asChild variant="outline" size="sm">
              {/* External-style anchor so the browser uses Content-Disposition. */}
              <a href={csvUrl} download={`darwin-${id}-ledger.csv`}>
                <Download className="mr-1 size-4" /> Ledger CSV
              </a>
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {isLoading ? <Skeleton className="h-56 w-full" /> : <TreasuryChart entries={entries} />}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Cost breakdown</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-3">
          <BillingMetric label="LLM" value={formatUsdc(billing?.llm_cost_usdc)} />
          <BillingMetric label="Sandbox" value={formatUsdc(billing?.sandbox_cost_usdc)} />
          <BillingMetric label="Browser" value={formatUsdc(billing?.browser_cost_usdc)} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Recent ledger entries</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {ledgerError ? (
            <QueryError error={ledgerErr} what="ledger entries" />
          ) : entries.length === 0 ? (
            <p className="px-4 pb-4 text-sm text-[var(--color-muted-foreground)]">No entries.</p>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-xs text-[var(--color-muted-foreground)]">
                <tr>
                  <th className="px-4 py-2 text-left font-normal">When</th>
                  <th className="px-4 py-2 text-left font-normal">Type</th>
                  <th className="px-4 py-2 text-left font-normal">Direction</th>
                  <th className="px-4 py-2 text-right font-normal">Amount</th>
                  <th className="px-4 py-2 text-left font-normal">Counterparty</th>
                </tr>
              </thead>
              <tbody>
                {entries.slice(0, 50).map((e) => (
                  <tr
                    key={`${e.seq}-${e.ts}`}
                    className="border-t border-[var(--color-border)]"
                  >
                    <td className="mono px-4 py-2 text-xs text-[var(--color-muted-foreground)]">
                      {formatTimestamp(e.ts)}
                    </td>
                    <td className="px-4 py-2">{e.type}</td>
                    <td className="px-4 py-2 text-xs text-[var(--color-muted-foreground)]">
                      {e.direction}
                    </td>
                    <td className="mono px-4 py-2 text-right tabular-nums">
                      {formatUsdc(e.amount_usdc)}
                    </td>
                    <td className="mono px-4 py-2 text-xs text-[var(--color-muted-foreground)]">
                      {e.counterparty ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function BillingMetric({ label, value }: { label: string; value: string }) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-[11px] uppercase tracking-wide text-[var(--color-muted-foreground)]">
          {label}
        </div>
        <div className="mono mt-1 text-xl font-semibold">{value}</div>
      </CardContent>
    </Card>
  );
}
