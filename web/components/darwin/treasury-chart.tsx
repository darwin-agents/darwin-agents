"use client";

import { useMemo } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { LedgerEntry } from "@/lib/types";

/**
 * Running treasury balance built from the ledger. The platform doesn't return
 * a precomputed balance series, so we reconstruct it by walking entries
 * oldest-first and accumulating amount × direction.
 */
export function TreasuryChart({ entries }: { entries: LedgerEntry[] }) {
  const series = useMemo(() => {
    const ordered = [...entries].sort((a, b) => {
      const at = new Date(a.ts).getTime();
      const bt = new Date(b.ts).getTime();
      if (at === bt) return a.seq - b.seq;
      return at - bt;
    });
    let running = 0;
    return ordered.map((e) => {
      const sign = e.direction === "credit" || e.direction === "in" ? 1 : -1;
      running += sign * Math.abs(e.amount_usdc);
      return {
        ts: new Date(e.ts).getTime(),
        balance: Number(running.toFixed(6)),
      };
    });
  }, [entries]);

  if (series.length === 0) {
    return (
      <p className="px-4 pb-4 text-sm text-[var(--color-muted-foreground)]">
        No ledger entries yet.
      </p>
    );
  }

  return (
    <div className="h-56 w-full">
      <ResponsiveContainer>
        <AreaChart data={series} margin={{ top: 12, right: 16, left: 4, bottom: 4 }}>
          <defs>
            <linearGradient id="treasuryFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--color-status-alive)" stopOpacity={0.4} />
              <stop offset="100%" stopColor="var(--color-status-alive)" stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
          <XAxis
            dataKey="ts"
            tickFormatter={(t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
            stroke="var(--color-muted-foreground)"
            tick={{ fontSize: 11 }}
          />
          <YAxis
            tickFormatter={(v) => `$${Number(v).toFixed(2)}`}
            stroke="var(--color-muted-foreground)"
            tick={{ fontSize: 11 }}
            width={64}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: "var(--color-card)",
              border: "1px solid var(--color-border)",
              fontSize: "12px",
            }}
            labelFormatter={(t) => new Date(Number(t)).toLocaleString()}
            formatter={(v: number) => [`$${v.toFixed(4)}`, "Balance"]}
          />
          <Area
            type="monotone"
            dataKey="balance"
            stroke="var(--color-status-alive)"
            strokeWidth={1.5}
            fill="url(#treasuryFill)"
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
