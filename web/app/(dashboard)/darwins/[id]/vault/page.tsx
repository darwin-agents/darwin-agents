"use client";

import Link from "next/link";
import { use } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Lock, Trash2, User, Bot } from "lucide-react";
import { api, type ApiError } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { formatRelative } from "@/lib/format";

interface VaultEntry {
  darwin_id: string;
  tier: "public" | "scoped" | "sensitive" | string;
  scope: string | null;
  key: string;
  path: string;
  sha256: string | null;
  size_bytes: number | null;
  uploaded_at: number;
  uploaded_by: string | null;
  expires_at: number | null;
  watermarked: boolean;
}

const TIER_BADGE: Record<string, string> = {
  public: "bg-[var(--color-muted)]/60 text-[var(--color-muted-foreground)]",
  scoped: "bg-[var(--color-alert-warning)]/20 text-[var(--color-alert-warning)]",
  sensitive: "bg-[var(--color-destructive)]/20 text-[var(--color-destructive)]",
};

export default function VaultPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const queryClient = useQueryClient();

  const queryKey = ["darwin", id, "vault"] as const;

  const { data, isLoading, isError, error } = useQuery({
    queryKey,
    queryFn: () => api.listVault(id),
  });

  const remove = useMutation({
    mutationFn: ({ tier, key }: { tier: string; key: string }) =>
      api.deleteVault(id, tier, key),
    onSettled: () => queryClient.invalidateQueries({ queryKey }),
  });

  const entries = ((data?.entries ?? []) as unknown as VaultEntry[]).slice().sort(
    (a, b) => b.uploaded_at - a.uploaded_at,
  );

  const ownerProvided = entries.filter((e) => isOwner(e.uploaded_by));
  const darwinCreated = entries.filter((e) => !isOwner(e.uploaded_by));

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4">
      <Link
        href={{ pathname: `/darwins/${id}` }}
        className="inline-flex items-center gap-1 text-xs text-[var(--color-muted-foreground)] hover:underline"
      >
        <ArrowLeft className="size-3" /> Back to Darwin
      </Link>

      {isError && (
        <p className="rounded border border-[var(--color-destructive)]/40 bg-[var(--color-destructive)]/10 px-3 py-2 text-sm text-[var(--color-destructive)]">
          {(error as ApiError).message ?? "Failed to load vault."}
        </p>
      )}

      <Section
        title="Owner-provided"
        icon={User}
        empty="Nothing here. Upload secrets via CLI for now."
        loading={isLoading}
        entries={ownerProvided}
        onRevoke={(e) => remove.mutate({ tier: e.tier, key: e.key })}
        revokingPath={remove.variables ? findPath(entries, remove.variables) : null}
      />
      <Section
        title="Darwin-created"
        icon={Bot}
        empty="The Darwin hasn't written any vault entries yet."
        loading={isLoading}
        entries={darwinCreated}
        onRevoke={(e) => remove.mutate({ tier: e.tier, key: e.key })}
        revokingPath={remove.variables ? findPath(entries, remove.variables) : null}
      />

      <p className="text-xs text-[var(--color-muted-foreground)]">
        Secret values are never returned by the API. Fingerprints + last-modified are the only
        identifying surface.
      </p>
    </div>
  );
}

function Section({
  title,
  icon: Icon,
  empty,
  loading,
  entries,
  onRevoke,
  revokingPath,
}: {
  title: string;
  icon: React.ComponentType<{ className?: string }>;
  empty: string;
  loading: boolean;
  entries: VaultEntry[];
  onRevoke: (entry: VaultEntry) => void;
  revokingPath: string | null;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Icon className="size-4 text-[var(--color-muted-foreground)]" />
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        {loading ? (
          <div className="space-y-2 p-3">
            {Array.from({ length: 2 }).map((_, i) => (
              <Skeleton key={i} className="h-12 w-full" />
            ))}
          </div>
        ) : entries.length === 0 ? (
          <p className="px-4 pb-4 text-sm text-[var(--color-muted-foreground)]">{empty}</p>
        ) : (
          <ul className="divide-y divide-[var(--color-border)]">
            {entries.map((e) => (
              <li key={e.path} className="grid grid-cols-[auto_minmax(0,1fr)_auto] gap-3 px-4 py-3">
                <span
                  className={`mt-0.5 inline-flex h-fit items-center gap-1 rounded-full px-2 py-0.5 text-[10px] uppercase ${
                    TIER_BADGE[e.tier] ?? TIER_BADGE.public
                  }`}
                >
                  {e.tier === "sensitive" && <Lock className="size-3" />}
                  {e.tier}
                </span>
                <div className="min-w-0">
                  <div className="flex items-baseline gap-2">
                    <span className="mono truncate text-sm">{e.key}</span>
                    {e.scope && (
                      <span className="mono text-xs text-[var(--color-muted-foreground)]">
                        · {e.scope}
                      </span>
                    )}
                  </div>
                  <div className="mt-1 flex flex-wrap gap-x-3 text-[11px] text-[var(--color-muted-foreground)]">
                    {e.sha256 && <span className="mono">sha256 {e.sha256.slice(0, 12)}…</span>}
                    {e.size_bytes !== null && <span>{e.size_bytes} B</span>}
                    {e.uploaded_at && <span>uploaded {formatRelative(e.uploaded_at)}</span>}
                    {e.expires_at && (
                      <span className="text-[var(--color-alert-warning)]">
                        expires {formatRelative(e.expires_at)}
                      </span>
                    )}
                    {e.watermarked && <span>watermarked</span>}
                  </div>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label="Revoke"
                  disabled={revokingPath === e.path}
                  onClick={() => onRevoke(e)}
                >
                  <Trash2 className="size-4" />
                </Button>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function isOwner(uploadedBy: string | null): boolean {
  if (!uploadedBy) return false;
  const lower = uploadedBy.toLowerCase();
  if (lower.includes("@")) return true;        // email-like
  if (lower.startsWith("owner") || lower.startsWith("user")) return true;
  if (lower === "anonymous") return true;       // auth-disabled mode
  return false;
}

function findPath(entries: VaultEntry[], k: { tier: string; key: string }): string | null {
  return entries.find((e) => e.tier === k.tier && e.key === k.key)?.path ?? null;
}
