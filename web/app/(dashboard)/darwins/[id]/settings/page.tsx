"use client";

import Link from "next/link";
import { use } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * Owner controls — M2-A first cut. Spend lock, reproduction block, and
 * disabled-neuron / disabled-category surfaces are wired against the existing
 * /controls endpoints. Harvest rules CRUD is M2-A finishing work.
 */
export default function SettingsPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: queryKeys.controls(id),
    queryFn: () => api.controls(id),
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: queryKeys.controls(id) });

  const setSpendLock = useMutation({
    mutationFn: (locked: boolean) => api.setSpendLock(id, locked),
    onSettled: invalidate,
  });
  const setReproBlock = useMutation({
    mutationFn: (blocked: boolean) => api.setReproductionBlock(id, blocked),
    onSettled: invalidate,
  });
  const forceReproduce = useMutation({
    mutationFn: () => api.forceReproduce(id),
  });

  const spendLocked = !!data?.spend_lock?.locked;
  const reproBlocked = !!data?.reproduction_block?.blocked;

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4">
      <Link
        href={{ pathname: `/darwins/${id}` }}
        className="inline-flex items-center gap-1 text-xs text-[var(--color-muted-foreground)] hover:underline"
      >
        <ArrowLeft className="size-3" /> Back to Darwin
      </Link>

      <Card>
        <CardHeader>
          <CardTitle>Owner controls</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {isLoading ? (
            <Skeleton className="h-32 w-full" />
          ) : (
            <>
              <Toggle
                label="Spend lock"
                description="Outbound payments refused. Metered LLM and compute still run."
                checked={spendLocked}
                disabled={setSpendLock.isPending}
                onChange={(v) => setSpendLock.mutate(v)}
              />
              <Toggle
                label="Block reproduction"
                description="Natural reproduction skipped. Platform force-reproduce still overrides."
                checked={reproBlocked}
                disabled={setReproBlock.isPending}
                onChange={(v) => setReproBlock.mutate(v)}
              />
              <div className="rounded-md border border-[var(--color-border)] p-3">
                <div className="font-medium">Force reproduce</div>
                <p className="text-xs text-[var(--color-muted-foreground)]">
                  Forces a reproduction cycle right now. Use sparingly — the platform applies the
                  normal admission and quota checks.
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-2"
                  disabled={forceReproduce.isPending}
                  onClick={() => forceReproduce.mutate()}
                >
                  {forceReproduce.isPending ? "Requesting…" : "Reproduce now"}
                </Button>
                {forceReproduce.isError && (
                  <p className="mt-2 text-xs text-[var(--color-destructive)]">
                    {(forceReproduce.error as Error).message}
                  </p>
                )}
              </div>
            </>
          )}

          {data?.disabled_neurons && data.disabled_neurons.length > 0 && (
            <Section title="Disabled neurons">
              <div className="flex flex-wrap gap-1">
                {data.disabled_neurons.map((n) => (
                  <span
                    key={n}
                    className="mono rounded-full bg-[var(--color-muted)] px-2 py-0.5 text-xs"
                  >
                    {n}
                  </span>
                ))}
              </div>
            </Section>
          )}
          {data?.disabled_categories && data.disabled_categories.length > 0 && (
            <Section title="Disabled categories">
              <div className="flex flex-wrap gap-1">
                {data.disabled_categories.map((n) => (
                  <span key={n} className="rounded-full bg-[var(--color-muted)] px-2 py-0.5 text-xs">
                    {n}
                  </span>
                ))}
              </div>
            </Section>
          )}
        </CardContent>
      </Card>

      <p className="text-xs text-[var(--color-muted-foreground)]">
        Multi-select editors for disabled neurons / categories, harvest rules CRUD, and force
        harvest land in M2-A. Lifecycle controls (pause, resume, terminate) live in the Darwin
        header.
      </p>
    </div>
  );
}

function Toggle({
  label,
  description,
  checked,
  disabled,
  onChange,
}: {
  label: string;
  description: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <label className="flex cursor-pointer items-start justify-between gap-4 rounded-md border border-[var(--color-border)] p-3">
      <div>
        <div className="font-medium">{label}</div>
        <p className="text-xs text-[var(--color-muted-foreground)]">{description}</p>
      </div>
      <input
        type="checkbox"
        className="mt-1 size-5 accent-[var(--color-accent)]"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
    </label>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-xs uppercase tracking-wide text-[var(--color-muted-foreground)]">
        {title}
      </div>
      {children}
    </div>
  );
}
