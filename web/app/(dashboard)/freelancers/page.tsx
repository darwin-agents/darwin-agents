"use client";

/**
 * /me/freelancers — owner self-serve view of verified fiat-payout
 * destinations (doc 17 §"Darwin paying a human"). Each entry is the
 * KYC gate for a freelancer/contractor the owner has onboarded; only
 * registered destinations can be reached by darwin_fiat_payment.
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BadgeCheck, Trash2, UserPlus } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import type { KycStartRequest, RegisterFreelancerRequest } from "@/lib/types";

const KYC_TIERS: NonNullable<KycStartRequest["tier"]>[] = [
  "basic",
  "standard",
  "enhanced",
];

const KYC_OK = new Set(["approved", "verified"]);

/**
 * Owner identity verification (`/platform/kyc/*`). KYC gates non-anonymous
 * owner actions (omnibus funding, fiat payouts). Starting a verification
 * returns a provider redirect URL we open in a new tab; status reflects the
 * latest verdict on the owner's own subject.
 */
function OwnerKycCard() {
  const qc = useQueryClient();
  const [tier, setTier] =
    useState<NonNullable<KycStartRequest["tier"]>>("standard");

  const status = useQuery({
    // Empty subject = the authenticated owner (server fills ctx.subject).
    queryKey: queryKeys.kycStatus("me"),
    queryFn: () => api.kycStatus(),
    retry: false,
  });

  const start = useMutation({
    mutationFn: () => api.kycStart({ tier }),
    onSuccess: (res) => {
      if (res.redirect_url && typeof window !== "undefined") {
        window.open(res.redirect_url, "_blank", "noopener,noreferrer");
      }
      qc.invalidateQueries({ queryKey: queryKeys.kycStatus("me") });
    },
  });

  const verdict = status.data;
  const current = (verdict?.status ?? "unknown").toLowerCase();
  const verified = KYC_OK.has(current);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <BadgeCheck
            className={
              verified
                ? "size-4 text-[var(--color-status-alive)]"
                : "size-4 text-[var(--color-muted-foreground)]"
            }
          />
          Your identity verification
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-xs text-[var(--color-muted-foreground)]">
          KYC unlocks non-anonymous owner actions — omnibus funding and fiat
          payouts. Verification runs through the platform&apos;s configured
          provider.
        </p>
        {status.isLoading ? (
          <Skeleton className="h-6 w-40" />
        ) : (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-[var(--color-muted-foreground)]">Status:</span>
            <Badge variant={verified ? "default" : current === "pending" ? "outline" : "destructive"}>
              {current}
            </Badge>
            {verdict?.tier && (
              <span className="text-xs text-[var(--color-muted-foreground)]">
                tier {verdict.tier}
              </span>
            )}
            {verdict?.reason && current !== "approved" && (
              <span className="text-xs text-[var(--color-muted-foreground)]">
                · {verdict.reason}
              </span>
            )}
          </div>
        )}
        <div className="flex flex-wrap items-end gap-2">
          <label className="text-xs">
            <span className="mb-1 block text-[var(--color-muted-foreground)]">Tier</span>
            <select
              value={tier}
              onChange={(e) =>
                setTier(e.target.value as NonNullable<KycStartRequest["tier"]>)
              }
              className="h-9 rounded-md border border-[var(--color-border)] bg-[var(--color-background)] px-3 text-sm"
            >
              {KYC_TIERS.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          <Button size="sm" disabled={start.isPending} onClick={() => start.mutate()}>
            {start.isPending
              ? "Starting…"
              : verified
                ? "Re-verify"
                : "Start verification"}
          </Button>
        </div>
        {start.isError && (
          <p className="text-xs text-[var(--color-destructive)]">
            {start.error instanceof ApiError ? start.error.message : String(start.error)}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function KycBadge({ status }: { status?: string }) {
  const s = (status ?? "unverified").toLowerCase();
  const variant: "default" | "outline" | "destructive" =
    s === "verified" ? "default" : s === "pending" ? "outline" : "destructive";
  return <Badge variant={variant}>{s}</Badge>;
}

export default function FreelancersPage() {
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.myFreelancers(),
    queryFn: () => api.listMyFreelancers(),
  });

  const [form, setForm] = useState<RegisterFreelancerRequest>({
    owner_subject: "", // overridden server-side by ctx.subject
    freelancer_id: "",
    fiat_destination: "",
    display_name: "",
    kyc_status: "unverified",
  });
  const [formError, setFormError] = useState<string | null>(null);

  const register = useMutation({
    mutationFn: (req: RegisterFreelancerRequest) => api.registerMyFreelancer(req),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.myFreelancers() });
      setForm({
        owner_subject: "",
        freelancer_id: "",
        fiat_destination: "",
        display_name: "",
        kyc_status: "unverified",
      });
      setFormError(null);
    },
    onError: (err: unknown) => {
      if (err instanceof ApiError) {
        setFormError(err.message);
      } else {
        setFormError(String(err));
      }
    },
  });

  const revoke = useMutation({
    mutationFn: (freelancerId: string) => api.revokeMyFreelancer(freelancerId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.myFreelancers() });
    },
  });

  const rows = useMemo(() => data ?? [], [data]);

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Freelancers</h1>
        <p className="mt-1 text-sm text-[var(--color-muted-foreground)]">
          Verified fiat-payout destinations. Only registered entries can be
          reached by a Darwin&apos;s fiat payment.
        </p>
      </header>

      <OwnerKycCard />

      {/* Existing entries */}
      <Card>
        <CardHeader>
          <CardTitle>Registered ({rows.length})</CardTitle>
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
          ) : rows.length === 0 ? (
            <div className="p-4 text-sm text-[var(--color-muted-foreground)]">
              No destinations registered yet.
            </div>
          ) : (
            <ul className="divide-y divide-[var(--color-border)]">
              {rows.map((row) => (
                <li
                  key={row.freelancer_id}
                  className="flex items-center justify-between gap-4 p-4"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-sm">
                        {row.display_name || row.freelancer_id}
                      </span>
                      <KycBadge status={row.kyc_status} />
                    </div>
                    <div className="mt-1 text-xs text-[var(--color-muted-foreground)]">
                      id: <code>{row.freelancer_id}</code>
                      <span className="mx-2">·</span>
                      destination: <code>{row.fiat_destination}</code>
                    </div>
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => revoke.mutate(row.freelancer_id)}
                    disabled={revoke.isPending}
                    aria-label={`Revoke ${row.freelancer_id}`}
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {/* Add new */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <UserPlus className="size-4" />
            Register a destination
          </CardTitle>
        </CardHeader>
        <CardContent>
          <form
            className="grid grid-cols-1 gap-3 sm:grid-cols-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (!form.freelancer_id.trim() || !form.fiat_destination.trim()) {
                setFormError("freelancer_id and fiat_destination are required");
                return;
              }
              register.mutate(form);
            }}
          >
            <label className="text-xs">
              <span className="mb-1 block text-[var(--color-muted-foreground)]">
                Freelancer id
              </span>
              <Input
                value={form.freelancer_id}
                onChange={(e) =>
                  setForm((f) => ({ ...f, freelancer_id: e.target.value }))
                }
                placeholder="upwork:writer-42"
                required
              />
            </label>
            <label className="text-xs">
              <span className="mb-1 block text-[var(--color-muted-foreground)]">
                Display name
              </span>
              <Input
                value={form.display_name ?? ""}
                onChange={(e) =>
                  setForm((f) => ({ ...f, display_name: e.target.value }))
                }
                placeholder="Jane Writer"
              />
            </label>
            <label className="text-xs sm:col-span-2">
              <span className="mb-1 block text-[var(--color-muted-foreground)]">
                Fiat destination (Stripe Connect acct, MoonPay token, …)
              </span>
              <Input
                value={form.fiat_destination}
                onChange={(e) =>
                  setForm((f) => ({ ...f, fiat_destination: e.target.value }))
                }
                placeholder="acct_1Q..."
                required
              />
            </label>
            <label className="text-xs">
              <span className="mb-1 block text-[var(--color-muted-foreground)]">
                KYC status
              </span>
              <select
                value={form.kyc_status ?? "unverified"}
                onChange={(e) =>
                  setForm((f) => ({ ...f, kyc_status: e.target.value }))
                }
                className="h-9 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-background)] px-3 text-sm"
              >
                <option value="unverified">unverified</option>
                <option value="pending">pending</option>
                <option value="verified">verified</option>
              </select>
            </label>

            {formError && (
              <div className="text-xs text-[var(--color-destructive)] sm:col-span-2">
                {formError}
              </div>
            )}

            <div className="sm:col-span-2">
              <Button type="submit" disabled={register.isPending}>
                {register.isPending ? "Registering…" : "Register"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
