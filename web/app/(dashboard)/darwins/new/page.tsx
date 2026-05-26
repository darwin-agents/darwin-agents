"use client";

import Link from "next/link";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, AlertTriangle } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export default function SpawnPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [budget, setBudget] = useState("100");
  const [acknowledged, setAcknowledged] = useState(false);

  const spawn = useMutation({
    mutationFn: () => api.spawn({ opening_usdc: Number(budget) }),
    onSuccess: (status) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.darwins() });
      router.push(`/darwins/${status.darwin_id}`);
    },
  });

  const budgetNumber = Number(budget);
  const budgetValid = Number.isFinite(budgetNumber) && budgetNumber > 0;

  return (
    <div className="mx-auto flex max-w-xl flex-col gap-4">
      <Link
        href={{ pathname: "/" }}
        className="inline-flex items-center gap-1 text-xs text-[var(--color-muted-foreground)] hover:underline"
      >
        <ArrowLeft className="size-3" /> Back to fleet
      </Link>

      <Card>
        <CardHeader>
          <CardTitle>Spawn a Darwin</CardTitle>
          <p className="text-sm text-[var(--color-muted-foreground)]">
            You fund an opening treasury. The Darwin acts autonomously inside it. If it depletes,
            it dies and the remaining funds are settled per the death sequence.
          </p>
        </CardHeader>
        <CardContent className="space-y-5">
          <div>
            <label htmlFor="budget" className="mb-1 block text-xs uppercase tracking-wide text-[var(--color-muted-foreground)]">
              Opening budget (USDC)
            </label>
            <Input
              id="budget"
              type="number"
              min="1"
              step="0.01"
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
              disabled={spawn.isPending}
            />
            {!budgetValid && budget.length > 0 && (
              <p className="mt-1 text-xs text-[var(--color-destructive)]">Enter a positive amount.</p>
            )}
          </div>

          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              className="mt-0.5 size-4 accent-[var(--color-accent)]"
              checked={acknowledged}
              onChange={(e) => setAcknowledged(e.target.checked)}
              disabled={spawn.isPending}
            />
            <span>
              I understand outcomes are non-guaranteed, the Darwin can lose its entire treasury,
              and there is no resurrection.
            </span>
          </label>

          {spawn.isError && (
            <SpawnError error={spawn.error} />
          )}

          <Button
            disabled={!budgetValid || !acknowledged || spawn.isPending}
            onClick={() => spawn.mutate()}
            className="w-full"
          >
            {spawn.isPending ? "Spawning…" : "Spawn Darwin"}
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}

function SpawnError({ error }: { error: unknown }) {
  let msg = "Unknown error";
  if (error instanceof ApiError) {
    if (error.status === 422) {
      msg = "Budget out of range. Check platform minimum / maximum.";
    } else {
      msg = `${error.status} · ${error.code}`;
    }
  } else if (error instanceof Error) {
    msg = error.message;
  }
  return (
    <div className="flex items-start gap-2 rounded-md border border-[var(--color-destructive)]/40 bg-[var(--color-destructive)]/10 px-3 py-2 text-sm text-[var(--color-destructive)]">
      <AlertTriangle className="mt-0.5 size-4 shrink-0" />
      <span>{msg}</span>
    </div>
  );
}
