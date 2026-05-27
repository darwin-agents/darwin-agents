"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { formatRelative, formatUsdc } from "@/lib/format";

interface HarvestRule {
  id: string;
  destination: string;
  trigger_balance_usdc: number;
  amount_usdc: number | null;
  interval_seconds: number;
  enabled: boolean;
  created_at: number;
  last_run_at: number | null;
  last_amount_usdc: number | null;
}

export function HarvestRules({ darwinId }: { darwinId: string }) {
  const queryClient = useQueryClient();
  const ruleQuery = useQuery({
    queryKey: ["darwin", darwinId, "harvest-rules"] as const,
    queryFn: () => api.listHarvestRules(darwinId),
    refetchInterval: 60_000,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["darwin", darwinId, "harvest-rules"] });

  const deleteRule = useMutation({
    mutationFn: (ruleId: string) => api.deleteHarvestRule(darwinId, ruleId),
    onSuccess: invalidate,
  });

  const rules = (ruleQuery.data?.rules ?? []) as unknown as HarvestRule[];

  return (
    <div className="space-y-3">
      {rules.length === 0 && !ruleQuery.isLoading ? (
        <p className="text-sm text-[var(--color-muted-foreground)]">
          No harvest rules. The Darwin will only release funds via a manual force-harvest.
        </p>
      ) : (
        <ul className="divide-y divide-[var(--color-border)] rounded-md border border-[var(--color-border)]">
          {rules.map((r) => (
            <li key={r.id} className="flex items-start justify-between gap-3 px-3 py-2 text-sm">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <span className="font-medium">
                    {r.amount_usdc !== null ? formatUsdc(r.amount_usdc) : "Sweep above trigger"}
                  </span>
                  <span className="text-xs text-[var(--color-muted-foreground)]">
                    trigger ≥ {formatUsdc(r.trigger_balance_usdc)}
                  </span>
                  <span className="text-xs text-[var(--color-muted-foreground)]">
                    every {Math.round(r.interval_seconds / 60)}m
                  </span>
                </div>
                <div className="mono mt-1 truncate text-xs text-[var(--color-muted-foreground)]">
                  → {r.destination}
                </div>
                {r.last_run_at && (
                  <div className="text-[11px] text-[var(--color-muted-foreground)]">
                    last run {formatRelative(r.last_run_at)}
                    {r.last_amount_usdc !== null && ` · ${formatUsdc(r.last_amount_usdc)}`}
                  </div>
                )}
              </div>
              <Button
                variant="ghost"
                size="icon"
                aria-label="Delete rule"
                disabled={deleteRule.isPending && deleteRule.variables === r.id}
                onClick={() => deleteRule.mutate(r.id)}
              >
                <Trash2 className="size-4" />
              </Button>
            </li>
          ))}
        </ul>
      )}

      {deleteRule.isError && (
        <p className="text-xs text-[var(--color-destructive)]">
          Couldn&apos;t delete: {(deleteRule.error as Error).message}
        </p>
      )}

      <AddRuleForm darwinId={darwinId} onAdded={invalidate} />
    </div>
  );
}

function AddRuleForm({ darwinId, onAdded }: { darwinId: string; onAdded: () => void }) {
  const [destination, setDestination] = useState("");
  const [trigger, setTrigger] = useState("");
  const [amount, setAmount] = useState("");
  const [intervalMinutes, setIntervalMinutes] = useState("60");

  const add = useMutation({
    mutationFn: () =>
      api.addHarvestRule(darwinId, {
        destination: destination.trim(),
        trigger_balance_usdc: Number(trigger),
        amount_usdc: amount.trim() === "" ? null : Number(amount),
        interval_seconds: Math.max(60, Math.round(Number(intervalMinutes) * 60)),
      }),
    onSuccess: () => {
      setDestination("");
      setTrigger("");
      setAmount("");
      onAdded();
    },
  });

  const triggerNum = Number(trigger);
  const intervalNum = Number(intervalMinutes);
  const amountNum = amount.trim() === "" ? null : Number(amount);
  const valid =
    destination.trim().length > 0 &&
    Number.isFinite(triggerNum) &&
    triggerNum > 0 &&
    Number.isFinite(intervalNum) &&
    intervalNum >= 1 &&
    (amountNum === null || (Number.isFinite(amountNum) && amountNum > 0));

  return (
    <form
      className="space-y-2 rounded-md border border-dashed border-[var(--color-border)] p-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid) add.mutate();
      }}
    >
      <div className="text-xs font-medium uppercase tracking-wide text-[var(--color-muted-foreground)]">
        Add rule
      </div>
      <div className="grid gap-2 sm:grid-cols-4">
        <Input
          placeholder="Destination (address)"
          className="sm:col-span-2 font-mono text-xs"
          value={destination}
          onChange={(e) => setDestination(e.target.value)}
          disabled={add.isPending}
        />
        <Input
          type="number"
          min="0"
          step="0.01"
          placeholder="Trigger (USDC)"
          value={trigger}
          onChange={(e) => setTrigger(e.target.value)}
          disabled={add.isPending}
        />
        <Input
          type="number"
          min="0"
          step="0.01"
          placeholder="Amount (blank = sweep above)"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          disabled={add.isPending}
        />
      </div>
      <div className="flex items-center gap-2">
        <Input
          type="number"
          min="1"
          placeholder="Interval (min)"
          className="w-32"
          value={intervalMinutes}
          onChange={(e) => setIntervalMinutes(e.target.value)}
          disabled={add.isPending}
        />
        <Button type="submit" size="sm" disabled={!valid || add.isPending}>
          <Plus className="mr-1 size-4" />
          {add.isPending ? "Adding…" : "Add"}
        </Button>
      </div>
      {add.isError && (
        <p className="text-xs text-[var(--color-destructive)]">
          {add.error instanceof ApiError && add.error.status === 422
            ? "Rule invalid — check destination + values."
            : (add.error as Error).message}
        </p>
      )}
    </form>
  );
}
