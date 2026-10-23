"use client";

import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowDownToLine, ArrowUpFromLine } from "lucide-react";
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

interface FiatQuote {
  quote_id: string;
  amount_usd: number;
  amount_usdc: number;
  fee_usd: number;
  rate: number;
  provider: string;
  ttl_seconds: number;
  quoted_at: number;
  // Hosted on-ramp (MoonPay): the buyer completes payment there and the
  // USDC is delivered straight to the Darwin's wallet.
  widget_url?: string | null;
}

type Mode = "deposit" | "withdraw";

export function FiatDialog({ darwinId }: { darwinId: string | null }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<Mode>("deposit");

  // Deposit is owner-scoped; withdraw is `/platform/fiat/withdraw`
  // (PLATFORM_OPERATOR). Hide the withdraw toggle for owners so they never
  // land on a panel that would only 403.
  const { data: caps } = useQuery({
    queryKey: queryKeys.capabilities(),
    queryFn: () => api.capabilities(),
    staleTime: 5 * 60_000,
    retry: false,
  });
  const canWithdraw = caps?.is_operator ?? false;
  // If caps flip to non-operator while parked on withdraw, fall back.
  const activeMode: Mode = mode === "withdraw" && !canWithdraw ? "deposit" : mode;

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          {activeMode === "deposit" ? (
            <ArrowDownToLine className="mr-1 size-4" />
          ) : (
            <ArrowUpFromLine className="mr-1 size-4" />
          )}
          {canWithdraw ? "Deposit / withdraw" : "Deposit"}
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Fiat ramp</DialogTitle>
          <DialogDescription>
            Provider is chosen at platform deploy time (<code>--fiat-provider</code>). The dashboard
            surfaces the quote — actual settlement happens via the configured ramp (Stripe or
            MoonPay).
          </DialogDescription>
        </DialogHeader>

        {canWithdraw && (
          <div className="flex gap-2">
            <ModeButton current={activeMode} value="deposit" onClick={setMode}>
              Deposit
            </ModeButton>
            <ModeButton current={activeMode} value="withdraw" onClick={setMode}>
              Withdraw
            </ModeButton>
          </div>
        )}

        {activeMode === "deposit" ? (
          <DepositPanel darwinId={darwinId} onDone={() => setOpen(false)} />
        ) : (
          <WithdrawPanel onDone={() => setOpen(false)} />
        )}
      </DialogContent>
    </Dialog>
  );
}

function ModeButton({
  current,
  value,
  onClick,
  children,
}: {
  current: Mode;
  value: Mode;
  onClick: (v: Mode) => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={() => onClick(value)}
      className={
        current === value
          ? "rounded-md bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-[var(--color-accent-foreground)]"
          : "rounded-md border border-[var(--color-border)] px-3 py-1.5 text-xs text-[var(--color-muted-foreground)] hover:bg-[var(--color-muted)]"
      }
    >
      {children}
    </button>
  );
}

function DepositPanel({ darwinId, onDone }: { darwinId: string | null; onDone: () => void }) {
  const [amount, setAmount] = useState("");
  const [method, setMethod] = useState("credit_debit_card");
  const [quote, setQuote] = useState<FiatQuote | null>(null);

  const fetchQuote = useMutation({
    mutationFn: () =>
      api.fiatDepositQuote({
        amount_usd: Number(amount),
        payment_method: method.trim(),
        destination_darwin_id: darwinId,
      }) as Promise<unknown> as Promise<FiatQuote>,
    onSuccess: (q) => setQuote(q),
  });

  const confirm = useMutation({
    mutationFn: () =>
      api.fiatConfirmDeposit({
        quote_id: quote!.quote_id,
        destination_darwin_id: darwinId,
      }),
    onSuccess: () => {
      onDone();
      setQuote(null);
      setAmount("");
    },
  });

  const amt = Number(amount);
  const valid = Number.isFinite(amt) && amt > 0 && method.trim().length > 0;

  return (
    <div className="space-y-3 text-sm">
      <Field label="Amount (USD)">
        <Input
          type="number"
          min="0"
          step="0.01"
          value={amount}
          onChange={(e) => {
            setAmount(e.target.value);
            setQuote(null);
          }}
          placeholder="100.00"
          disabled={fetchQuote.isPending || confirm.isPending}
        />
      </Field>
      <Field label="Payment method (provider-defined)">
        <Input
          value={method}
          onChange={(e) => {
            setMethod(e.target.value);
            setQuote(null);
          }}
          placeholder="card / sepa / wire"
          disabled={fetchQuote.isPending || confirm.isPending}
        />
      </Field>

      {!quote ? (
        <Button
          className="w-full"
          disabled={!valid || fetchQuote.isPending}
          onClick={() => fetchQuote.mutate()}
        >
          {fetchQuote.isPending ? "Quoting…" : "Get quote"}
        </Button>
      ) : (
        <QuoteSummary
          quote={quote}
          target={darwinId}
          confirming={confirm.isPending}
          onConfirm={() => confirm.mutate()}
          onReset={() => setQuote(null)}
        />
      )}

      {(fetchQuote.isError || confirm.isError) && (
        <ErrorPanel error={fetchQuote.error ?? confirm.error} />
      )}
    </div>
  );
}

function QuoteSummary({
  quote,
  target,
  confirming,
  onConfirm,
  onReset,
}: {
  quote: FiatQuote;
  target: string | null;
  confirming: boolean;
  onConfirm: () => void;
  onReset: () => void;
}) {
  return (
    <div className="space-y-3 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-3 text-xs">
      <div className="grid grid-cols-2 gap-1">
        <span className="text-[var(--color-muted-foreground)]">Provider</span>
        <span className="text-right capitalize">{quote.provider}</span>
        <span className="text-[var(--color-muted-foreground)]">Quoted</span>
        <span className="mono text-right">${quote.amount_usd.toFixed(2)} USD</span>
        <span className="text-[var(--color-muted-foreground)]">Fee</span>
        <span className="mono text-right">${quote.fee_usd.toFixed(2)} USD</span>
        <span className="text-[var(--color-muted-foreground)]">Net credit</span>
        <span className="mono text-right font-semibold text-[var(--color-status-alive)]">
          {formatUsdc(quote.amount_usdc)}
        </span>
        <span className="text-[var(--color-muted-foreground)]">TTL</span>
        <span className="text-right">{Math.round(quote.ttl_seconds)}s</span>
        {target && (
          <>
            <span className="text-[var(--color-muted-foreground)]">Credits Darwin</span>
            <span className="mono text-right">{target}</span>
          </>
        )}
      </div>
      {quote.widget_url && (
        <p className="text-[var(--color-muted-foreground)]">
          Pay with {quote.provider}; the USDC goes straight to this Darwin&apos;s wallet and shows
          up here once it confirms on-chain.
        </p>
      )}
      <DialogFooter>
        <Button variant="outline" disabled={confirming} onClick={onReset}>
          Edit
        </Button>
        {quote.widget_url ? (
          <Button asChild>
            <a href={quote.widget_url} target="_blank" rel="noopener noreferrer">
              Continue to {quote.provider}
            </a>
          </Button>
        ) : (
          <Button disabled={confirming} onClick={onConfirm}>
            {confirming ? "Confirming…" : "Confirm deposit"}
          </Button>
        )}
      </DialogFooter>
    </div>
  );
}

function WithdrawPanel({ onDone }: { onDone: () => void }) {
  const [amount, setAmount] = useState("");
  const [destination, setDestination] = useState("");

  const withdraw = useMutation({
    mutationFn: () =>
      api.fiatWithdraw({
        amount_usdc: Number(amount),
        destination: destination.trim(),
      }),
    onSuccess: () => {
      onDone();
      setAmount("");
      setDestination("");
    },
  });

  const amt = Number(amount);
  const valid = Number.isFinite(amt) && amt > 0 && destination.trim().length > 0;

  return (
    <div className="space-y-3 text-sm">
      <Field label="Amount (USDC)">
        <Input
          type="number"
          min="0"
          step="0.01"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          placeholder="0.00"
          disabled={withdraw.isPending}
        />
      </Field>
      <Field label="Destination (bank / payment method id)">
        <Input
          value={destination}
          onChange={(e) => setDestination(e.target.value)}
          placeholder="bank-account-id"
          disabled={withdraw.isPending}
          className="font-mono"
        />
      </Field>
      <Button
        className="w-full"
        disabled={!valid || withdraw.isPending}
        onClick={() => withdraw.mutate()}
      >
        {withdraw.isPending ? "Withdrawing…" : "Withdraw"}
      </Button>
      {withdraw.isError && <ErrorPanel error={withdraw.error} />}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="mb-1 block text-xs tracking-wide text-[var(--color-muted-foreground)] uppercase">
        {label}
      </label>
      {children}
    </div>
  );
}

function ErrorPanel({ error }: { error: unknown }) {
  let msg = "Unknown error";
  if (error instanceof ApiError) {
    msg =
      error.status === 409
        ? "Quote expired or provider refused."
        : `${error.status} · ${error.code}`;
  } else if (error instanceof Error) {
    msg = error.message;
  }
  return (
    <div className="rounded border border-[var(--color-destructive)]/40 bg-[var(--color-destructive)]/10 px-3 py-2 text-xs text-[var(--color-destructive)]">
      {msg}
    </div>
  );
}
