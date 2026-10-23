"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDownToLine } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { formatUsdc } from "@/lib/format";

/**
 * Owner-initiated harvest. Mutating money flow — Idempotency-Key is per
 * dialog open so re-pressing the button never moves funds twice.
 */
export function HarvestDialog({ darwinId }: { darwinId: string }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [amount, setAmount] = useState("");
  const [destination, setDestination] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState(() => crypto.randomUUID());

  const { data: status } = useQuery({
    queryKey: queryKeys.darwin(darwinId),
    queryFn: () => api.getDarwin(darwinId),
    enabled: open,
  });
  const balance = status?.balance_usdc;
  const profit = status?.profit_usdc;
  const payout = status?.payout_address ?? "";

  const mutation = useMutation({
    mutationFn: () =>
      api.harvest(
        darwinId,
        { amount_usdc: Number(amount), destination: destination.trim() },
        idempotencyKey,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.billing(darwinId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.ledger(darwinId, 0) });
      queryClient.invalidateQueries({ queryKey: queryKeys.events(darwinId, 0) });
      setOpen(false);
      setAmount("");
      setDestination("");
    },
  });

  const amt = Number(amount);
  const amountValid = Number.isFinite(amt) && amt > 0 && (balance === undefined || amt <= balance);
  const destValid = destination.trim().length > 0;

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (mutation.isPending) return;
        setOpen(o);
        if (o) {
          setIdempotencyKey(crypto.randomUUID());
          if (!destination && payout) setDestination(payout);
        }
      }}
    >
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <ArrowDownToLine className="mr-1 size-4" />
          Force harvest
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Force harvest</DialogTitle>
          <DialogDescription>
            Move funds out of the Darwin&apos;s wallet to a destination address. The Darwin loses
            this balance immediately — it cannot be re-funded except via deposit.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-sm">
          <div className="flex items-center justify-between rounded-md border border-[var(--color-border)] px-3 py-2 text-xs">
            <span className="text-[var(--color-muted-foreground)]">Available</span>
            <span className="mono">{formatUsdc(balance)}</span>
          </div>
          <p className="text-xs text-[var(--color-muted-foreground)]">
            Profit now {formatUsdc(profit)}. The platform fee applies only to the profit part of a
            harvest; withdrawing your deposit is free.
          </p>
          <Field
            label="Amount (USDC)"
            input={
              <Input
                type="number"
                min="0"
                step="0.01"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                placeholder="0.00"
                disabled={mutation.isPending}
              />
            }
            hint={
              balance !== undefined && Number.isFinite(amt) && amt > balance
                ? "Exceeds available balance."
                : undefined
            }
          />
          <Field
            label="Destination"
            input={
              <Input
                value={destination}
                onChange={(e) => setDestination(e.target.value)}
                placeholder="USDC address or wallet handle"
                disabled={mutation.isPending}
                className="font-mono"
              />
            }
          />
          {mutation.isError && <HarvestErrorPanel error={mutation.error} />}
        </div>

        <DialogFooter>
          <Button variant="outline" disabled={mutation.isPending} onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button
            disabled={!amountValid || !destValid || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? "Harvesting…" : "Confirm harvest"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Field({ label, input, hint }: { label: string; input: React.ReactNode; hint?: string }) {
  return (
    <div>
      <label className="mb-1 block text-xs tracking-wide text-[var(--color-muted-foreground)] uppercase">
        {label}
      </label>
      {input}
      {hint && <p className="mt-1 text-xs text-[var(--color-destructive)]">{hint}</p>}
    </div>
  );
}

function HarvestErrorPanel({ error }: { error: unknown }) {
  if (error instanceof ApiError && error.isConflict) {
    return (
      <div className="rounded border border-[var(--color-alert-throttle)]/40 bg-[var(--color-alert-throttle)]/10 px-3 py-2 text-xs text-[var(--color-alert-throttle)]">
        Harvest rejected — spend lock, insufficient balance, or policy refused. Check ledger.
      </div>
    );
  }
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="rounded border border-[var(--color-destructive)]/40 bg-[var(--color-destructive)]/10 px-3 py-2 text-xs text-[var(--color-destructive)]">
      {msg}
    </div>
  );
}
