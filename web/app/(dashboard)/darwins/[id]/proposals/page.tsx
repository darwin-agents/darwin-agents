"use client";

/**
 * Neuron-mutation proposals. The Darwin's evolution loop stages candidate
 * neurons here; the owner can see what's pending and what's been admitted
 * into the live organism. Admitting / rejecting is an operator action
 * (PLATFORM_OPERATOR) — the buttons only render for operator tokens; owners
 * get a read-only view.
 */

import { use, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { GitMerge, Check, X, Dna } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import type { ProposalNeuron } from "@/lib/types";

export default function ProposalsPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const qc = useQueryClient();

  const { data: caps } = useQuery({
    queryKey: queryKeys.capabilities(),
    queryFn: () => api.capabilities(),
    staleTime: 5 * 60_000,
    retry: false,
  });
  const isOperator = caps?.is_operator ?? false;

  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.proposals(id),
    queryFn: () => api.listProposals(id),
    refetchInterval: 30_000,
  });

  const [actionError, setActionError] = useState<string | null>(null);

  const admit = useMutation({
    mutationFn: (name: string) => api.admitProposal(id, name),
    onSuccess: () => {
      setActionError(null);
      qc.invalidateQueries({ queryKey: queryKeys.proposals(id) });
    },
    onError: (err: unknown) =>
      setActionError(err instanceof ApiError ? err.message : String(err)),
  });

  const reject = useMutation({
    mutationFn: ({ name, reason }: { name: string; reason: string }) =>
      api.rejectProposal(id, name, reason),
    onSuccess: () => {
      setActionError(null);
      qc.invalidateQueries({ queryKey: queryKeys.proposals(id) });
    },
    onError: (err: unknown) =>
      setActionError(err instanceof ApiError ? err.message : String(err)),
  });

  const pending = data?.pending ?? [];
  const admitted = data?.admitted ?? [];
  const busy = admit.isPending || reject.isPending;

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4">
      <header>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <GitMerge className="size-5 text-[var(--color-accent)]" />
          Proposals
        </h1>
        <p className="mt-1 text-sm text-[var(--color-muted-foreground)]">
          Candidate neuron mutations staged by the evolution loop.{" "}
          {isOperator
            ? "Admit to merge into the live organism, or reject."
            : "Read-only — admitting is an operator action."}
        </p>
      </header>

      {actionError && (
        <p className="rounded border border-[var(--color-destructive)]/40 bg-[var(--color-destructive)]/10 px-3 py-2 text-xs text-[var(--color-destructive)]">
          {actionError}
        </p>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Pending ({pending.length})</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="p-4">
              <Skeleton className="h-16 w-full" />
            </div>
          ) : error ? (
            <p className="p-4 text-sm text-[var(--color-destructive)]">
              Failed to load: {error instanceof Error ? error.message : String(error)}
            </p>
          ) : pending.length === 0 ? (
            <p className="p-4 text-sm text-[var(--color-muted-foreground)]">
              No pending proposals. The Darwin hasn&apos;t staged any mutations.
            </p>
          ) : (
            <ul className="divide-y divide-[var(--color-border)]">
              {pending.map((p) => (
                <ProposalRow key={p.name} proposal={p}>
                  {isOperator && (
                    <div className="flex gap-2">
                      <Button
                        size="sm"
                        disabled={busy}
                        onClick={() => admit.mutate(p.name)}
                      >
                        <Check className="mr-1 size-4" />
                        Admit
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busy}
                        onClick={() => {
                          const reason =
                            window.prompt(`Reject "${p.name}" — reason?`, "operator rejected") ??
                            null;
                          if (reason !== null) reject.mutate({ name: p.name, reason });
                        }}
                      >
                        <X className="mr-1 size-4" />
                        Reject
                      </Button>
                    </div>
                  )}
                </ProposalRow>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Dna className="size-4 text-[var(--color-muted-foreground)]" />
            Admitted ({admitted.length})
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {admitted.length === 0 ? (
            <p className="p-4 text-sm text-[var(--color-muted-foreground)]">
              Nothing admitted yet.
            </p>
          ) : (
            <ul className="divide-y divide-[var(--color-border)]">
              {admitted.map((p) => (
                <ProposalRow key={p.name} proposal={p} />
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function ProposalRow({
  proposal,
  children,
}: {
  proposal: ProposalNeuron;
  children?: React.ReactNode;
}) {
  return (
    <li className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 px-4 py-3">
      <div className="min-w-0">
        <div className="flex items-baseline gap-2">
          <span className="mono truncate text-sm font-medium">{proposal.name}</span>
          {proposal.version && (
            <span className="text-xs text-[var(--color-muted-foreground)]">
              v{proposal.version}
            </span>
          )}
        </div>
        {proposal.tags.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {proposal.tags.map((t) => (
              <Badge key={t} variant="outline" className="text-[10px]">
                {t}
              </Badge>
            ))}
          </div>
        )}
      </div>
      {children}
    </li>
  );
}
