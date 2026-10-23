"use client";

import { formatSignedUsdc, formatUsdc } from "@/lib/format";
import { Card } from "@/components/ui/card";
import type { BillingResponse, DarwinStatus } from "@/lib/types";

function asNumber(v: unknown): number | undefined {
  if (typeof v === "number") return v;
  if (typeof v === "string" && v.trim()) {
    const n = Number(v);
    return Number.isFinite(n) ? n : undefined;
  }
  return undefined;
}

export function TreasuryStrip({
  status,
  billing,
}: {
  status: DarwinStatus | undefined;
  billing: BillingResponse | undefined;
}) {
  const balance = asNumber(status?.balance_usdc);
  const profit = asNumber(status?.profit_usdc);
  const generation = asNumber(status?.generation);
  const spent = asNumber(billing?.infrastructure.gross_usdc);
  const unsettled = asNumber(status?.unsettled_usdc);
  const harvested = asNumber(billing?.harvests.net_usdc);

  return (
    <Card className="flex flex-wrap items-center gap-x-8 gap-y-2 p-4">
      <Metric label="Treasury" value={formatUsdc(balance)} mono />
      <Metric label="Profit" value={profit !== undefined ? formatSignedUsdc(profit) : "—"} />
      <Metric label="Generation" value={generation !== undefined ? `gen ${generation}` : "—"} />
      <div className="flex-1" />
      <div className="flex flex-wrap items-center gap-4 text-xs text-[var(--color-muted-foreground)]">
        <span>Spent {formatUsdc(spent)}</span>
        <span>Unsettled {formatUsdc(unsettled)}</span>
        <span>Harvested {formatUsdc(harvested)}</span>
      </div>
    </Card>
  );
}

function Metric({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex flex-col">
      <span className="text-[11px] tracking-wide text-[var(--color-muted-foreground)] uppercase">
        {label}
      </span>
      <span className={mono ? "mono text-lg font-semibold" : "text-lg font-semibold"}>{value}</span>
    </div>
  );
}
