"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, API_BASE_URL } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { formatUsdc } from "@/lib/format";

/** Where money comes in and goes out: the Darwin's own address (fund it,
 *  buyers pay it), the owner's payout address, and its storefront. */
export function MoneyCard({ darwinId }: { darwinId: string }) {
  const queryClient = useQueryClient();
  const { data: status } = useQuery({
    queryKey: queryKeys.darwin(darwinId),
    queryFn: () => api.getDarwin(darwinId),
  });
  const { data: store } = useQuery({
    queryKey: queryKeys.store(darwinId),
    queryFn: () => api.storeInfo(darwinId),
    retry: false, // 404/409 = no storefront here
  });
  const [payout, setPayout] = useState("");
  const save = useMutation({
    mutationFn: () => api.setPayoutAddress(darwinId, payout.trim()),
    onSuccess: () => {
      setPayout("");
      queryClient.invalidateQueries({ queryKey: queryKeys.darwin(darwinId) });
    },
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Money</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <Field label="Fund this Darwin (USDC on Base)">
          <Copyable value={status?.address} />
        </Field>

        <Field label="Payout address (harvests + closing sweep)">
          {status?.payout_address && <Copyable value={status.payout_address} />}
          <form
            className="mt-2 flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate();
            }}
          >
            <Input
              value={payout}
              onChange={(e) => setPayout(e.target.value)}
              placeholder="0x… (checksummed)"
              aria-label="payout address"
            />
            <Button
              type="submit"
              size="sm"
              disabled={payout.trim().length !== 42 || save.isPending}
            >
              {status?.payout_address ? "Change" : "Set"}
            </Button>
          </form>
          {save.isSuccess && (
            <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
              Saved. Usable for harvests from{" "}
              {new Date(save.data.usable_from * 1000).toLocaleString()}.
            </p>
          )}
          {save.isError && (
            <p className="mt-1 text-xs text-[var(--color-destructive)]">
              {save.error instanceof ApiError ? save.error.code : "could not save"}
            </p>
          )}
        </Field>

        {store && (
          <Field label={`Storefront · ${formatUsdc(store.price_usdc)} per report`}>
            <Copyable value={`${API_BASE_URL}/store/${darwinId}/report`} />
            <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
              Buyers pay over x402 straight to this Darwin&apos;s wallet; it writes the report.
            </p>
          </Field>
        )}
      </CardContent>
    </Card>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-[11px] tracking-wide text-[var(--color-muted-foreground)] uppercase">
        {label}
      </div>
      {children}
    </div>
  );
}

function Copyable({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="text-[var(--color-muted-foreground)]">—</span>;
  return (
    <button
      type="button"
      title="Copy"
      onClick={() => navigator.clipboard?.writeText(value)}
      className="mono block w-full truncate rounded border border-[var(--color-border)] px-2 py-1 text-left text-xs hover:bg-[var(--color-muted)]"
    >
      {value}
    </button>
  );
}
