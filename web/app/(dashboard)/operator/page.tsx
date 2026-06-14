"use client";

/**
 * Fleet-level operator console (PLATFORM_OPERATOR). Global levers that act
 * across every owned Darwin (pause/resume/spend-lock, category + counterparty
 * blocks), the patch-staging verdict history, and the platform signing key.
 * Operator-gated: the sidebar link and this body both check `caps.is_operator`.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Gauge,
  Pause,
  Play,
  Lock,
  Ban,
  FlaskConical,
  KeyRound,
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { OperatorOnly } from "@/components/layout/operator-only";
import { MoneyCapabilitiesCard } from "@/components/operator/money-capabilities";
import { formatRelative } from "@/lib/format";

export default function FleetOperatorPage() {
  const { data: caps, isLoading } = useQuery({
    queryKey: queryKeys.capabilities(),
    queryFn: () => api.capabilities(),
    staleTime: 5 * 60_000,
    retry: false,
  });

  if (isLoading) {
    return (
      <div className="mx-auto max-w-4xl">
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }
  if (caps && !caps.is_operator) {
    return <OperatorOnly title="Operator console" />;
  }

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4">
      <header>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <Gauge className="size-5 text-[var(--color-accent)]" />
          Operator console
        </h1>
        <p className="mt-1 text-sm text-[var(--color-muted-foreground)]">
          Fleet-wide controls, patch-staging verdicts, and the platform signing key.
        </p>
      </header>

      <MoneyCapabilitiesCard />
      <FleetControlsCard />
      <StagingCard />
      <SigningCard />
    </div>
  );
}

function ResultLine({ text }: { text: string | null }) {
  if (!text) return null;
  return <p className="text-xs text-[var(--color-status-alive)]">{text}</p>;
}
function ErrorLine({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof ApiError ? error.message : String(error);
  return <p className="text-xs text-[var(--color-destructive)]">{msg}</p>;
}

/* -- fleet controls ------------------------------------------------------ */
function FleetControlsCard() {
  const qc = useQueryClient();
  const invalidateFleet = () =>
    qc.invalidateQueries({ queryKey: queryKeys.darwins() });
  const [msg, setMsg] = useState<string | null>(null);
  const [category, setCategory] = useState("");
  const [address, setAddress] = useState("");

  const pause = useMutation({
    mutationFn: () => api.fleetPause(),
    onSuccess: (r) => {
      setMsg(`Paused ${r.paused?.length ?? 0} Darwins.`);
      invalidateFleet();
    },
  });
  const resume = useMutation({
    mutationFn: () => api.fleetResume(),
    onSuccess: (r) => {
      setMsg(`Resumed ${r.resumed?.length ?? 0} Darwins.`);
      invalidateFleet();
    },
  });
  const spendLock = useMutation({
    mutationFn: (locked: boolean) => api.fleetSpendLock(locked),
    onSuccess: (r) => {
      const n = (r.spend_locked ?? r.spend_unlocked ?? []).length;
      setMsg(`${r.spend_locked ? "Locked" : "Unlocked"} spend on ${n} Darwins.`);
      invalidateFleet();
    },
  });
  const blockCategory = useMutation({
    mutationFn: (c: string) => api.fleetBlockCategory(c),
    onSuccess: (r) => {
      setMsg(`Blocked "${r.category}" on ${r.blocked_on.length} Darwins.`);
      setCategory("");
    },
  });
  const blockCounterparty = useMutation({
    mutationFn: (a: string) => api.fleetBlockCounterparty(a),
    onSuccess: (r) => {
      setMsg(`Counterparty ${r.address} blocked: ${r.blocked}.`);
      setAddress("");
    },
  });

  const busy =
    pause.isPending ||
    resume.isPending ||
    spendLock.isPending ||
    blockCategory.isPending ||
    blockCounterparty.isPending;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Fleet controls</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" disabled={busy} onClick={() => pause.mutate()}>
            <Pause className="mr-1 size-4" /> Pause all
          </Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => resume.mutate()}>
            <Play className="mr-1 size-4" /> Resume all
          </Button>
          <Button
            size="sm"
            variant="destructive"
            disabled={busy}
            onClick={() => spendLock.mutate(true)}
          >
            <Lock className="mr-1 size-4" /> Spend-lock all
          </Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => spendLock.mutate(false)}>
            Unlock spend
          </Button>
        </div>

        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (category.trim()) blockCategory.mutate(category.trim());
          }}
        >
          <label className="text-xs">
            <span className="mb-1 block text-[var(--color-muted-foreground)]">
              Block spend category (fleet-wide)
            </span>
            <Input
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              placeholder="e.g. ad_network"
              className="h-8 w-48"
            />
          </label>
          <Button type="submit" size="sm" variant="outline" disabled={busy || !category.trim()}>
            <Ban className="mr-1 size-4" /> Block
          </Button>
        </form>

        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (address.trim()) blockCounterparty.mutate(address.trim());
          }}
        >
          <label className="text-xs">
            <span className="mb-1 block text-[var(--color-muted-foreground)]">
              Block counterparty address (fleet-wide)
            </span>
            <Input
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              placeholder="0x… / wallet handle"
              className="h-8 w-64 font-mono"
            />
          </label>
          <Button type="submit" size="sm" variant="outline" disabled={busy || !address.trim()}>
            <Ban className="mr-1 size-4" /> Block
          </Button>
        </form>

        <ResultLine text={msg} />
        <ErrorLine
          error={
            pause.error ||
            resume.error ||
            spendLock.error ||
            blockCategory.error ||
            blockCounterparty.error
          }
        />
      </CardContent>
    </Card>
  );
}

/* -- patch staging ------------------------------------------------------- */
function StagingCard() {
  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.stagingStatus(),
    queryFn: () => api.stagingStatus(),
    refetchInterval: 30_000,
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <FlaskConical className="size-4 text-[var(--color-muted-foreground)]" />
          Patch staging
        </CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        {isLoading ? (
          <div className="p-4">
            <Skeleton className="h-24 w-full" />
          </div>
        ) : error ? (
          <p className="p-4 text-sm text-[var(--color-destructive)]">
            {error instanceof Error ? error.message : String(error)}
          </p>
        ) : (
          <div>
            <div className="grid grid-cols-2 gap-3 px-4 py-3 text-xs sm:grid-cols-4">
              <Stat label="Concurrent" value={`${data?.quota.active ?? 0}/${data?.quota.max_concurrent ?? 0}`} />
              <Stat label="Acquired" value={String(data?.quota.total_acquired ?? 0)} />
              <Stat label="Rejected" value={String(data?.quota.total_rejected ?? 0)} />
              <Stat label="History" value={String(data?.history_size ?? 0)} />
            </div>
            {(data?.recent ?? []).length === 0 ? (
              <p className="px-4 pb-4 text-sm text-[var(--color-muted-foreground)]">
                No recent verdicts.
              </p>
            ) : (
              <ul className="divide-y divide-[var(--color-border)] border-t border-[var(--color-border)]">
                {data!.recent.slice(0, 20).map((v, i) => (
                  <li key={`${v.sandbox_session_id}-${i}`} className="flex items-center justify-between gap-3 px-4 py-2 text-xs">
                    <div className="min-w-0">
                      <Badge variant={v.promote ? "default" : "destructive"}>
                        {v.promote ? "promote" : "reject"}
                      </Badge>{" "}
                      <span className="text-[var(--color-muted-foreground)]">{v.reason}</span>
                    </div>
                    <span className="mono shrink-0 text-[var(--color-muted-foreground)]">
                      {v.baseline_score.toFixed(2)} → {v.candidate_score.toFixed(2)} · ${v.sandbox_cost_usdc.toFixed(4)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-[var(--color-muted-foreground)]">
        {label}
      </div>
      <div className="mono text-sm">{value}</div>
    </div>
  );
}

/* -- signing key --------------------------------------------------------- */
function SigningCard() {
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.signingKey(),
    queryFn: () => api.signingPublicKey(),
  });
  const [rotated, setRotated] = useState<string | null>(null);

  const rotate = useMutation({
    mutationFn: () => api.rotateSigningKey(),
    onSuccess: (r) => {
      setRotated(
        `Rotated. Old key drains until ${r.drain_until ? formatRelative(r.drain_until) : "—"}.`,
      );
      qc.invalidateQueries({ queryKey: queryKeys.signingKey() });
    },
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <KeyRound className="size-4 text-[var(--color-muted-foreground)]" />
          Signing key
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {isLoading ? (
          <Skeleton className="h-20 w-full" />
        ) : error ? (
          <p className="text-sm text-[var(--color-destructive)]">
            {error instanceof Error ? error.message : String(error)}
          </p>
        ) : (
          <>
            <div className="text-xs text-[var(--color-muted-foreground)]">
              Format: {data?.format}
            </div>
            <pre className="mono max-h-40 overflow-auto rounded bg-[var(--color-background)] p-2 text-[10px]">
              {data?.public_key}
            </pre>
          </>
        )}
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="destructive"
            disabled={rotate.isPending}
            onClick={() => {
              if (
                window.confirm(
                  "Rotate the platform signing key? The current key enters a drain window before retirement.",
                )
              ) {
                rotate.mutate();
              }
            }}
          >
            {rotate.isPending ? "Rotating…" : "Rotate key"}
          </Button>
        </div>
        <ResultLine text={rotated} />
        <ErrorLine error={rotate.error} />
      </CardContent>
    </Card>
  );
}
