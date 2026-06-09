"use client";

/**
 * Per-Darwin pre-approvals manager (doc 17 §"Owner pre-approvals").
 * Lists active rules, lets the owner install / revoke. Shows max_uses
 * progress so the consume counter is visible at a glance.
 */

import { use, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck, Trash2 } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { OperatorOnly } from "@/components/layout/operator-only";
import type { PreApprovalRequest } from "@/lib/types";

const CATEGORIES = [
  "freelancer",
  "legal_financial",
  "owner",
  "api",
  "infra",
  "llm",
  "ad_network",
  "exchange",
  "unknown",
] as const;

function emptyForm(): PreApprovalRequest & { rule_id: string } {
  return {
    rule_id: "",
    max_amount_usdc: 50,
    counterparty_category: null,
    counterparty_substring: null,
    expires_at: null,
    max_uses: null,
  };
}

function UsageBadge({
  used,
  max,
}: {
  used: number;
  max: number | null | undefined;
}) {
  if (max == null) return <Badge variant="outline">{used} uses</Badge>;
  const exhausted = used >= max;
  return (
    <Badge variant={exhausted ? "destructive" : "default"}>
      {used} / {max}
    </Badge>
  );
}

function ExpiresLabel({ ts }: { ts?: number | null }) {
  if (ts == null) return <span className="text-[var(--color-muted-foreground)]">no expiry</span>;
  const now = Date.now() / 1000;
  const remaining = ts - now;
  if (remaining <= 0) return <span className="text-[var(--color-destructive)]">expired</span>;
  const days = Math.floor(remaining / 86400);
  const hours = Math.floor((remaining % 86400) / 3600);
  return (
    <span className="text-[var(--color-muted-foreground)]">
      expires in {days}d {hours}h
    </span>
  );
}

export default function PreApprovalsPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const qc = useQueryClient();

  // Operator-only console — the nav tab is hidden for owners; this guards
  // direct-URL access. Hooks below still run; we swap the render once caps
  // resolve.
  const { data: caps } = useQuery({
    queryKey: queryKeys.capabilities(),
    queryFn: () => api.capabilities(),
    staleTime: 5 * 60_000,
    retry: false,
  });

  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.preapprovals(id),
    queryFn: () => api.listPreapprovals(id),
  });

  const [form, setForm] = useState(emptyForm());
  const [formError, setFormError] = useState<string | null>(null);

  const install = useMutation({
    mutationFn: ({ rule_id, ...body }: typeof form) =>
      api.setPreapproval(id, rule_id, body as PreApprovalRequest),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.preapprovals(id) });
      setForm(emptyForm());
      setFormError(null);
    },
    onError: (err: unknown) => {
      setFormError(err instanceof ApiError ? err.message : String(err));
    },
  });

  const revoke = useMutation({
    mutationFn: (ruleId: string) => api.revokePreapproval(id, ruleId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.preapprovals(id) });
    },
  });

  const rules = useMemo(() => data ?? [], [data]);

  if (caps && !caps.is_operator) {
    return <OperatorOnly title="Pre-approvals" />;
  }

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4">
      <header>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <ShieldCheck className="size-5 text-[var(--color-accent)]" />
          Pre-approvals
        </h1>
        <p className="mt-1 text-sm text-[var(--color-muted-foreground)]">
          Typed exceptions to layer-4 escalation. A matching rule converts an
          escalation into an audit-logged allow.
        </p>
      </header>

      {/* Existing rules */}
      <Card>
        <CardHeader>
          <CardTitle>Active ({rules.length})</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="p-4">
              <Skeleton className="h-16 w-full" />
            </div>
          ) : error ? (
            <div className="p-4 text-sm text-[var(--color-destructive)]">
              Failed to load: {error instanceof Error ? error.message : String(error)}
            </div>
          ) : rules.length === 0 ? (
            <div className="p-4 text-sm text-[var(--color-muted-foreground)]">
              No pre-approvals installed. Every freelancer / legal_financial /
              over-threshold spend will escalate.
            </div>
          ) : (
            <ul className="divide-y divide-[var(--color-border)]">
              {rules.map((r) => (
                <li
                  key={r.rule_id}
                  className="flex items-start justify-between gap-4 p-4"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-sm">{r.rule_id}</span>
                      <UsageBadge used={r.used_count} max={r.max_uses} />
                    </div>
                    <div className="mt-1 text-xs text-[var(--color-muted-foreground)]">
                      ≤ ${r.max_amount_usdc.toFixed(2)}
                      {r.counterparty_category && (
                        <>
                          <span className="mx-2">·</span>
                          category: <code>{r.counterparty_category}</code>
                        </>
                      )}
                      {r.counterparty_substring && (
                        <>
                          <span className="mx-2">·</span>
                          matches: <code>{r.counterparty_substring}</code>
                        </>
                      )}
                      <span className="mx-2">·</span>
                      <ExpiresLabel ts={r.expires_at} />
                    </div>
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => revoke.mutate(r.rule_id)}
                    disabled={revoke.isPending}
                    aria-label={`Revoke ${r.rule_id}`}
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {/* New rule */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Install a rule</CardTitle>
        </CardHeader>
        <CardContent>
          <form
            className="grid grid-cols-1 gap-3 sm:grid-cols-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (!form.rule_id.trim()) {
                setFormError("rule_id is required");
                return;
              }
              if (!(form.max_amount_usdc > 0)) {
                setFormError("max_amount_usdc must be > 0");
                return;
              }
              install.mutate(form);
            }}
          >
            <label className="text-xs">
              <span className="mb-1 block text-[var(--color-muted-foreground)]">
                Rule id
              </span>
              <Input
                value={form.rule_id}
                onChange={(e) =>
                  setForm((f) => ({ ...f, rule_id: e.target.value }))
                }
                placeholder="upwork-under-50"
                required
              />
            </label>
            <label className="text-xs">
              <span className="mb-1 block text-[var(--color-muted-foreground)]">
                Max amount (USDC)
              </span>
              <Input
                type="number"
                min={0.01}
                step={0.01}
                value={form.max_amount_usdc}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    max_amount_usdc: Number(e.target.value),
                  }))
                }
                required
              />
            </label>
            <label className="text-xs">
              <span className="mb-1 block text-[var(--color-muted-foreground)]">
                Counterparty category (optional)
              </span>
              <select
                value={form.counterparty_category ?? ""}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    counterparty_category: e.target.value || null,
                  }))
                }
                className="h-9 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-background)] px-3 text-sm"
              >
                <option value="">— any —</option>
                {CATEGORIES.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-xs">
              <span className="mb-1 block text-[var(--color-muted-foreground)]">
                Counterparty substring (optional)
              </span>
              <Input
                value={form.counterparty_substring ?? ""}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    counterparty_substring: e.target.value || null,
                  }))
                }
                placeholder="upwork"
              />
            </label>
            <label className="text-xs">
              <span className="mb-1 block text-[var(--color-muted-foreground)]">
                Max uses (optional)
              </span>
              <Input
                type="number"
                min={1}
                step={1}
                value={form.max_uses ?? ""}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    max_uses: e.target.value ? Number(e.target.value) : null,
                  }))
                }
                placeholder="unlimited"
              />
            </label>
            <label className="text-xs">
              <span className="mb-1 block text-[var(--color-muted-foreground)]">
                Expires at (epoch seconds, optional)
              </span>
              <Input
                type="number"
                min={1}
                step={1}
                value={form.expires_at ?? ""}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    expires_at: e.target.value ? Number(e.target.value) : null,
                  }))
                }
                placeholder="no expiry"
              />
            </label>

            {formError && (
              <div className="text-xs text-[var(--color-destructive)] sm:col-span-2">
                {formError}
              </div>
            )}

            <div className="sm:col-span-2">
              <Button type="submit" disabled={install.isPending}>
                {install.isPending ? "Installing…" : "Install"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
