"use client";

/**
 * Per-Darwin fiat payments console (doc 17). Three operator actions
 * stacked into one page:
 *
 *  1. Send a fiat payment — select a registered freelancer, amount,
 *     optional escalation_id. Refuses if the freelancer isn't in the
 *     owner's registry (KYC gate).
 *  2. Refund a failed payout — recovery for leg-2 failures; chain
 *     transfer omnibus → Darwin wallet, undoing leg-1.
 *  3. Retry skipped omnibus fund — operator-driven recovery when the
 *     spawn-time fund_child hit a transient chain error.
 */

import { use, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDownToLine, CreditCard, RotateCcw } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";

function FlashLine({
  kind,
  text,
}: {
  kind: "ok" | "err" | null;
  text: string | null;
}) {
  if (!kind || !text) return null;
  const color =
    kind === "ok"
      ? "text-[var(--color-status-alive)]"
      : "text-[var(--color-destructive)]";
  return <p className={`text-xs ${color}`}>{text}</p>;
}

export default function DarwinPaymentsPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const qc = useQueryClient();

  const { data: freelancers, isLoading: loadingFreelancers } = useQuery({
    queryKey: queryKeys.myFreelancers(),
    queryFn: () => api.listMyFreelancers(),
  });

  // -- pay form ---------------------------------------------------------
  const [payForm, setPayForm] = useState({
    freelancer_id: "",
    amount_usdc: 0,
    escalation_id: "",
  });
  const [payFlash, setPayFlash] = useState<{
    kind: "ok" | "err" | null;
    text: string | null;
  }>({ kind: null, text: null });

  const pay = useMutation({
    mutationFn: () =>
      api.darwinFiatPayment(id, {
        amount_usdc: payForm.amount_usdc,
        freelancer_id: payForm.freelancer_id,
        escalation_id: payForm.escalation_id || null,
      }),
    onSuccess: (entry) => {
      setPayFlash({
        kind: "ok",
        text: `Settled ${entry.amount_usdc.toFixed(2)} USDC → ${entry.metadata?.fiat_destination ?? "fiat destination"}`,
      });
      setPayForm({ freelancer_id: "", amount_usdc: 0, escalation_id: "" });
      qc.invalidateQueries({ queryKey: queryKeys.billing(id) });
      qc.invalidateQueries({ queryKey: queryKeys.ledger(id) });
    },
    onError: (err: unknown) => {
      setPayFlash({
        kind: "err",
        text: err instanceof ApiError ? err.message : String(err),
      });
    },
  });

  // -- refund form ------------------------------------------------------
  const [refundForm, setRefundForm] = useState({
    amount_usdc: 0,
    reason: "fiat_payout_leg2_failed",
  });
  const [refundFlash, setRefundFlash] = useState<{
    kind: "ok" | "err" | null;
    text: string | null;
  }>({ kind: null, text: null });

  const refund = useMutation({
    mutationFn: () =>
      api.darwinFiatRefund(id, {
        amount_usdc: refundForm.amount_usdc,
        reason: refundForm.reason,
      }),
    onSuccess: (entry) => {
      setRefundFlash({
        kind: "ok",
        text: `Refunded ${entry.amount_usdc.toFixed(2)} USDC to wallet`,
      });
      setRefundForm((f) => ({ ...f, amount_usdc: 0 }));
      qc.invalidateQueries({ queryKey: queryKeys.billing(id) });
    },
    onError: (err: unknown) => {
      setRefundFlash({
        kind: "err",
        text: err instanceof ApiError ? err.message : String(err),
      });
    },
  });

  // -- retry fund form --------------------------------------------------
  const [retryForm, setRetryForm] = useState({ amount_usdc: 0 });
  const [retryFlash, setRetryFlash] = useState<{
    kind: "ok" | "err" | null;
    text: string | null;
  }>({ kind: null, text: null });

  const retry = useMutation({
    mutationFn: () =>
      api.retryOmnibusFund(id, { amount_usdc: retryForm.amount_usdc }),
    onSuccess: (entry) => {
      setRetryFlash({
        kind: "ok",
        text: `Funded ${entry.amount_usdc.toFixed(2)} USDC from user_credits`,
      });
      setRetryForm({ amount_usdc: 0 });
      qc.invalidateQueries({ queryKey: queryKeys.billing(id) });
    },
    onError: (err: unknown) => {
      setRetryFlash({
        kind: "err",
        text: err instanceof ApiError ? err.message : String(err),
      });
    },
  });

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4">
      <header>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <CreditCard className="size-5 text-[var(--color-accent)]" />
          Payments
        </h1>
        <p className="mt-1 text-sm text-[var(--color-muted-foreground)]">
          Operator controls for the Darwin&apos;s fiat path: send, refund,
          retry omnibus fund.
        </p>
      </header>

      {/* Send fiat payment */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Send a fiat payment</CardTitle>
        </CardHeader>
        <CardContent>
          <form
            className="grid grid-cols-1 gap-3 sm:grid-cols-2"
            onSubmit={(e) => {
              e.preventDefault();
              setPayFlash({ kind: null, text: null });
              if (!payForm.freelancer_id) {
                setPayFlash({ kind: "err", text: "select a freelancer" });
                return;
              }
              if (!(payForm.amount_usdc > 0)) {
                setPayFlash({ kind: "err", text: "amount must be > 0" });
                return;
              }
              pay.mutate();
            }}
          >
            <label className="text-xs sm:col-span-2">
              <span className="mb-1 block text-[var(--color-muted-foreground)]">
                Freelancer
              </span>
              {loadingFreelancers ? (
                <Skeleton className="h-9 w-full" />
              ) : (
                <select
                  value={payForm.freelancer_id}
                  onChange={(e) =>
                    setPayForm((f) => ({ ...f, freelancer_id: e.target.value }))
                  }
                  className="h-9 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-background)] px-3 text-sm"
                  required
                >
                  <option value="">— select —</option>
                  {(freelancers ?? []).map((f) => (
                    <option key={f.freelancer_id} value={f.freelancer_id}>
                      {f.display_name || f.freelancer_id} ·{" "}
                      {f.kyc_status ?? "unverified"}
                    </option>
                  ))}
                </select>
              )}
              {(freelancers ?? []).length === 0 && !loadingFreelancers && (
                <p className="mt-1 text-[10px] text-[var(--color-muted-foreground)]">
                  No registered freelancers — add one on the Freelancers page
                  first.
                </p>
              )}
            </label>
            <label className="text-xs">
              <span className="mb-1 block text-[var(--color-muted-foreground)]">
                Amount (USDC)
              </span>
              <Input
                type="number"
                min={0.01}
                step={0.01}
                value={payForm.amount_usdc || ""}
                onChange={(e) =>
                  setPayForm((f) => ({ ...f, amount_usdc: Number(e.target.value) }))
                }
                required
              />
            </label>
            <label className="text-xs">
              <span className="mb-1 block text-[var(--color-muted-foreground)]">
                Escalation id (optional)
              </span>
              <Input
                value={payForm.escalation_id}
                onChange={(e) =>
                  setPayForm((f) => ({ ...f, escalation_id: e.target.value }))
                }
                placeholder="esc-12345"
              />
            </label>
            <FlashLine kind={payFlash.kind} text={payFlash.text} />
            <div className="sm:col-span-2">
              <Button type="submit" disabled={pay.isPending}>
                {pay.isPending ? "Settling…" : "Send fiat payment"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {/* Refund a failed payout */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <ArrowDownToLine className="size-4" />
            Refund failed payout
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="mb-3 text-xs text-[var(--color-muted-foreground)]">
            Use when leg 2 (omnibus → fiat ramp) failed and the Darwin&apos;s
            USDC is parked at the omnibus address. Submits a chain transfer
            back to the wallet.
          </p>
          <form
            className="grid grid-cols-1 gap-3 sm:grid-cols-2"
            onSubmit={(e) => {
              e.preventDefault();
              setRefundFlash({ kind: null, text: null });
              if (!(refundForm.amount_usdc > 0)) {
                setRefundFlash({ kind: "err", text: "amount must be > 0" });
                return;
              }
              refund.mutate();
            }}
          >
            <label className="text-xs">
              <span className="mb-1 block text-[var(--color-muted-foreground)]">
                Amount (USDC)
              </span>
              <Input
                type="number"
                min={0.01}
                step={0.01}
                value={refundForm.amount_usdc || ""}
                onChange={(e) =>
                  setRefundForm((f) => ({
                    ...f,
                    amount_usdc: Number(e.target.value),
                  }))
                }
                required
              />
            </label>
            <label className="text-xs">
              <span className="mb-1 block text-[var(--color-muted-foreground)]">
                Reason (audit metadata)
              </span>
              <Input
                value={refundForm.reason}
                onChange={(e) =>
                  setRefundForm((f) => ({ ...f, reason: e.target.value }))
                }
              />
            </label>
            <FlashLine kind={refundFlash.kind} text={refundFlash.text} />
            <div className="sm:col-span-2">
              <Button
                type="submit"
                variant="destructive"
                disabled={refund.isPending}
              >
                {refund.isPending ? "Refunding…" : "Refund to wallet"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {/* Retry omnibus fund */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <RotateCcw className="size-4" />
            Retry omnibus fund
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="mb-3 text-xs text-[var(--color-muted-foreground)]">
            Use when the spawn-time <code>fund_child</code> was skipped (chain
            outage or insufficient credit). Debits <code>user_credits</code>
            and submits the chain transfer now. Requires a non-anonymous
            owner.{" "}
            <Badge variant="outline" className="ml-1">
              audit: omnibus_fund_skipped
            </Badge>
          </p>
          <form
            className="grid grid-cols-1 gap-3 sm:grid-cols-2"
            onSubmit={(e) => {
              e.preventDefault();
              setRetryFlash({ kind: null, text: null });
              if (!(retryForm.amount_usdc > 0)) {
                setRetryFlash({ kind: "err", text: "amount must be > 0" });
                return;
              }
              retry.mutate();
            }}
          >
            <label className="text-xs">
              <span className="mb-1 block text-[var(--color-muted-foreground)]">
                Amount (USDC)
              </span>
              <Input
                type="number"
                min={0.01}
                step={0.01}
                value={retryForm.amount_usdc || ""}
                onChange={(e) =>
                  setRetryForm({ amount_usdc: Number(e.target.value) })
                }
                required
              />
            </label>
            <FlashLine kind={retryFlash.kind} text={retryFlash.text} />
            <div className="sm:col-span-2">
              <Button type="submit" disabled={retry.isPending}>
                {retry.isPending ? "Funding…" : "Retry fund"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
