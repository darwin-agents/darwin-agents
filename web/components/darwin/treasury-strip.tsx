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
  const balance =
    asNumber(status?.balance_usdc) ?? asNumber(status?.treasury_usdc) ?? asNumber(billing?.balance_usdc);
  const burn = asNumber(billing?.burn_rate_usdc_per_hour);
  const generation = asNumber(status?.generation);
  const llm = asNumber(billing?.llm_cost_usdc);
  const sandbox = asNumber(billing?.sandbox_cost_usdc);
  const browser = asNumber(billing?.browser_cost_usdc);

  return (
    <Card className="flex flex-wrap items-center gap-x-8 gap-y-2 p-4">
      <Metric label="Treasury" value={formatUsdc(balance)} mono />
      <Metric label="Burn / hr" value={burn !== undefined ? formatSignedUsdc(-Math.abs(burn)) : "—"} />
      <Metric label="Generation" value={generation !== undefined ? `gen ${generation}` : "—"} />
      <div className="flex-1" />
      <div className="flex items-center gap-4 text-xs text-[var(--color-muted-foreground)]">
        <span>LLM {formatUsdc(llm)}</span>
        <span>Sandbox {formatUsdc(sandbox)}</span>
        <span>Browser {formatUsdc(browser)}</span>
      </div>
    </Card>
  );
}

function Metric({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex flex-col">
      <span className="text-[11px] uppercase tracking-wide text-[var(--color-muted-foreground)]">
        {label}
      </span>
      <span className={mono ? "mono text-lg font-semibold" : "text-lg font-semibold"}>{value}</span>
    </div>
  );
}
