"use client";

/**
 * Per-Darwin operator console (PLATFORM_OPERATOR). Surfaces the operator-only
 * controls that act on a single Darwin: LLM-tier + resource throttles, the
 * wallet spend policy, and quarantine. Owner tokens never see this — the nav
 * tab is gated on `caps.is_operator` and the body falls back to OperatorOnly.
 */

import { use, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Gauge, ShieldAlert, Wallet, Cpu, GitBranch } from "lucide-react";
import Link from "next/link";
import { api, ApiError } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { OperatorOnly } from "@/components/layout/operator-only";
import type {
  LlmTier,
  ResourceKind,
  ResourceThrottleRow,
  WalletPolicy,
} from "@/lib/types";

const LLM_TIERS: LlmTier[] = ["fast", "reasoning", "coding", "image_gen", "embedding"];
const RESOURCE_KINDS: ResourceKind[] = ["sandbox", "browser", "egress"];

export default function OperatorConsolePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);

  const { data: caps, isLoading: capsLoading } = useQuery({
    queryKey: queryKeys.capabilities(),
    queryFn: () => api.capabilities(),
    staleTime: 5 * 60_000,
    retry: false,
  });

  if (capsLoading) {
    return (
      <div className="mx-auto max-w-4xl">
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }
  if (caps && !caps.is_operator) {
    return <OperatorOnly title="Operator controls" />;
  }

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4">
      <Link
        href={{ pathname: `/darwins/${id}` }}
        className="inline-flex items-center gap-1 text-xs text-[var(--color-muted-foreground)] hover:underline"
      >
        <ArrowLeft className="size-3" /> Back to Darwin
      </Link>

      <header>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <Gauge className="size-5 text-[var(--color-accent)]" />
          Operator controls
        </h1>
        <p className="mt-1 text-sm text-[var(--color-muted-foreground)]">
          Operator-only levers for this Darwin: throttles, wallet policy, quarantine.
        </p>
      </header>

      <LlmThrottleCard darwinId={id} />
      <ResourceThrottleCard darwinId={id} />
      <WalletPolicyCard darwinId={id} />
      <QuarantineCard darwinId={id} />
      <AmendmentCard darwinId={id} />
    </div>
  );
}

function ErrorLine({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof ApiError ? error.message : String(error);
  return <p className="text-xs text-[var(--color-destructive)]">{msg}</p>;
}

/* -- LLM tier throttles -------------------------------------------------- */
function LlmThrottleCard({ darwinId }: { darwinId: string }) {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: queryKeys.throttle(darwinId),
    queryFn: () => api.getThrottle(darwinId),
  });
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const set = useMutation({
    mutationFn: (req: { tier: LlmTier; max_daily_calls: number }) =>
      api.setThrottle(darwinId, req),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.throttle(darwinId) }),
  });
  const clear = useMutation({
    mutationFn: (tier: LlmTier) => api.deleteThrottle(darwinId, tier),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.throttle(darwinId) }),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Cpu className="size-4 text-[var(--color-muted-foreground)]" />
          LLM-tier daily call caps
        </CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-32 w-full" />
        ) : (
          <div className="space-y-2">
            {LLM_TIERS.map((tier) => {
              const current = data?.throttles[tier] ?? 0;
              const draft = drafts[tier];
              return (
                <div key={tier} className="grid grid-cols-[1fr_auto_auto_auto] items-center gap-2">
                  <span className="mono text-sm">{tier}</span>
                  <span className="text-xs text-[var(--color-muted-foreground)]">
                    current: {current === 0 ? "unlimited" : current}
                  </span>
                  <Input
                    type="number"
                    min={0}
                    className="h-8 w-28"
                    placeholder="max/day"
                    value={draft ?? ""}
                    onChange={(e) =>
                      setDrafts((d) => ({ ...d, [tier]: e.target.value }))
                    }
                  />
                  <div className="flex gap-1">
                    <Button
                      size="sm"
                      disabled={draft === undefined || draft === "" || set.isPending}
                      onClick={() =>
                        set.mutate({ tier, max_daily_calls: Number(draft) })
                      }
                    >
                      Set
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={clear.isPending}
                      onClick={() => clear.mutate(tier)}
                    >
                      Clear
                    </Button>
                  </div>
                </div>
              );
            })}
            <ErrorLine error={set.error || clear.error} />
            <p className="text-[11px] text-[var(--color-muted-foreground)]">
              0 / cleared = unlimited. Caps reset daily.
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/* -- resource throttles -------------------------------------------------- */
function ResourceThrottleCard({ darwinId }: { darwinId: string }) {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: queryKeys.resourceThrottle(darwinId),
    queryFn: () => api.getResourceThrottle(darwinId),
  });
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const set = useMutation({
    mutationFn: (req: { kind: ResourceKind; max_daily_calls: number }) =>
      api.setResourceThrottle(darwinId, req),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: queryKeys.resourceThrottle(darwinId) }),
  });
  const clear = useMutation({
    mutationFn: (kind: ResourceKind) => api.deleteResourceThrottle(darwinId, kind),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: queryKeys.resourceThrottle(darwinId) }),
  });

  const byKind = new Map<string, ResourceThrottleRow>(
    (data?.throttles ?? []).map((t) => [t.kind, t]),
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Gauge className="size-4 text-[var(--color-muted-foreground)]" />
          Resource daily call caps
        </CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : (
          <div className="space-y-2">
            {RESOURCE_KINDS.map((kind) => {
              const current = byKind.get(kind);
              const draft = drafts[kind];
              return (
                <div key={kind} className="grid grid-cols-[1fr_auto_auto_auto] items-center gap-2">
                  <span className="mono text-sm">{kind}</span>
                  <span className="text-xs text-[var(--color-muted-foreground)]">
                    current: {current ? current.max_daily_calls : "unlimited"}
                  </span>
                  <Input
                    type="number"
                    min={0}
                    className="h-8 w-28"
                    placeholder="max/day"
                    value={draft ?? ""}
                    onChange={(e) =>
                      setDrafts((d) => ({ ...d, [kind]: e.target.value }))
                    }
                  />
                  <div className="flex gap-1">
                    <Button
                      size="sm"
                      disabled={draft === undefined || draft === "" || set.isPending}
                      onClick={() => set.mutate({ kind, max_daily_calls: Number(draft) })}
                    >
                      Set
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={!current || clear.isPending}
                      onClick={() => clear.mutate(kind)}
                    >
                      Clear
                    </Button>
                  </div>
                </div>
              );
            })}
            <ErrorLine error={set.error || clear.error} />
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/* -- wallet policy ------------------------------------------------------- */
function WalletPolicyCard({ darwinId }: { darwinId: string }) {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: queryKeys.walletPolicy(darwinId),
    queryFn: () => api.getWalletPolicy(darwinId),
  });
  const [form, setForm] = useState<Record<keyof WalletPolicy, string>>({
    per_action_cap_usdc: "",
    per_day_cap_usdc: "",
    per_lifetime_cap_usdc: "",
  });

  // Seed the form once the policy loads.
  useEffect(() => {
    if (!data) return;
    setForm({
      per_action_cap_usdc: data.per_action_cap_usdc?.toString() ?? "",
      per_day_cap_usdc: data.per_day_cap_usdc?.toString() ?? "",
      per_lifetime_cap_usdc: data.per_lifetime_cap_usdc?.toString() ?? "",
    });
  }, [data]);

  const save = useMutation({
    mutationFn: () =>
      api.setWalletPolicy(darwinId, {
        per_action_cap_usdc: numOrNull(form.per_action_cap_usdc),
        per_day_cap_usdc: numOrNull(form.per_day_cap_usdc),
        per_lifetime_cap_usdc: numOrNull(form.per_lifetime_cap_usdc),
      }),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: queryKeys.walletPolicy(darwinId) }),
  });

  const FIELDS: { key: keyof WalletPolicy; label: string }[] = [
    { key: "per_action_cap_usdc", label: "Per-action cap (USDC)" },
    { key: "per_day_cap_usdc", label: "Per-day cap (USDC)" },
    { key: "per_lifetime_cap_usdc", label: "Lifetime cap (USDC)" },
  ];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Wallet className="size-4 text-[var(--color-muted-foreground)]" />
          Wallet spend policy
        </CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : (
          <form
            className="grid grid-cols-1 gap-3 sm:grid-cols-3"
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate();
            }}
          >
            {FIELDS.map((f) => (
              <label key={f.key} className="text-xs">
                <span className="mb-1 block text-[var(--color-muted-foreground)]">
                  {f.label}
                </span>
                <Input
                  type="number"
                  min={0}
                  step={0.01}
                  placeholder="none"
                  value={form[f.key]}
                  onChange={(e) =>
                    setForm((s) => ({ ...s, [f.key]: e.target.value }))
                  }
                />
              </label>
            ))}
            <div className="sm:col-span-3">
              <Button type="submit" disabled={save.isPending}>
                {save.isPending ? "Saving…" : "Save policy"}
              </Button>
              <ErrorLine error={save.error} />
            </div>
            <p className="text-[11px] text-[var(--color-muted-foreground)] sm:col-span-3">
              Empty = no cap. Caps are enforced platform-side on every spend.
            </p>
          </form>
        )}
      </CardContent>
    </Card>
  );
}

function numOrNull(s: string): number | null {
  if (s.trim() === "") return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

/* -- amendment reproduction (Path-C) ------------------------------------- */
function AmendmentCard({ darwinId }: { darwinId: string }) {
  const [name, setName] = useState("");
  const [reason, setReason] = useState("amendment reproduction");
  const [blobB64, setBlobB64] = useState<string | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const submit = useMutation({
    mutationFn: () =>
      api.reproduceWithAmendment(darwinId, {
        amendment_blob_b64: blobB64 ?? "",
        amendment_name: name.trim(),
        kill_parent_reason: reason.trim() || "amendment reproduction",
      }),
    onSuccess: () => {
      setDone("Amendment reproduction requested. The parent is killed and a child spawns with the patch.");
      setBlobB64(null);
      setFileName(null);
      setName("");
    },
  });

  async function onFile(file: File) {
    const buf = await file.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]!);
    setBlobB64(btoa(bin));
    setFileName(file.name);
    if (!name) setName(file.name);
  }

  const valid = name.trim().length > 0 && !!blobB64;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <GitBranch className="size-4 text-[var(--color-muted-foreground)]" />
          Reproduce with amendment (Path-C)
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-xs text-[var(--color-muted-foreground)]">
          Kill this Darwin and respawn a child carrying an operator-supplied
          patch. The amendment file is base64-encoded client-side and staged
          through the sandbox before promotion.
        </p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="text-xs">
            <span className="mb-1 block text-[var(--color-muted-foreground)]">
              Amendment file
            </span>
            <input
              type="file"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void onFile(f);
              }}
              className="block w-full text-xs file:mr-2 file:rounded file:border file:border-[var(--color-border)] file:bg-[var(--color-card)] file:px-2 file:py-1 file:text-xs"
            />
            {fileName && (
              <span className="mt-1 block text-[10px] text-[var(--color-muted-foreground)]">
                {fileName} · {blobB64?.length ?? 0} b64 chars
              </span>
            )}
          </label>
          <label className="text-xs">
            <span className="mb-1 block text-[var(--color-muted-foreground)]">
              Amendment name
            </span>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. payments-retry-fix"
              className="font-mono"
            />
          </label>
          <label className="text-xs sm:col-span-2">
            <span className="mb-1 block text-[var(--color-muted-foreground)]">
              Kill-parent reason (audit)
            </span>
            <Input value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
        </div>
        <Button
          variant="destructive"
          size="sm"
          disabled={!valid || submit.isPending}
          onClick={() => {
            if (
              window.confirm(
                "Kill this Darwin and respawn a child with the amendment? This is irreversible for the parent.",
              )
            ) {
              setDone(null);
              submit.mutate();
            }
          }}
        >
          {submit.isPending ? "Requesting…" : "Reproduce with amendment"}
        </Button>
        <ResultLine text={done} />
        <ErrorLine error={submit.error} />
      </CardContent>
    </Card>
  );
}

function ResultLine({ text }: { text: string | null }) {
  if (!text) return null;
  return <p className="text-xs text-[var(--color-status-alive)]">{text}</p>;
}

/* -- quarantine ---------------------------------------------------------- */
function QuarantineCard({ darwinId }: { darwinId: string }) {
  const qc = useQueryClient();
  const invalidate = () =>
    qc.invalidateQueries({ queryKey: queryKeys.darwin(darwinId) });

  const quarantine = useMutation({
    mutationFn: (reason: string) => api.quarantine(darwinId, { reason }),
    onSuccess: invalidate,
  });
  const clear = useMutation({
    mutationFn: () => api.clearQuarantine(darwinId),
    onSuccess: invalidate,
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <ShieldAlert className="size-4 text-[var(--color-destructive)]" />
          Quarantine
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-xs text-[var(--color-muted-foreground)]">
          Quarantine freezes the Darwin&apos;s outbound actions without killing it —
          use for suspected compromise or runaway behaviour pending review.
        </p>
        <div className="flex gap-2">
          <Button
            variant="destructive"
            size="sm"
            disabled={quarantine.isPending}
            onClick={() => {
              const reason = window.prompt("Quarantine reason?", "operator hold");
              if (reason) quarantine.mutate(reason);
            }}
          >
            Quarantine
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={clear.isPending}
            onClick={() => clear.mutate()}
          >
            Clear quarantine
          </Button>
        </div>
        <ErrorLine error={quarantine.error || clear.error} />
      </CardContent>
    </Card>
  );
}
