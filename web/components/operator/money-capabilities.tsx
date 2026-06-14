"use client";

/**
 * Money rails — read-only view of which payment rails this deployment actually
 * has wired (wallet provider + network, fiat ramp, sanctions, KYC, omnibus,
 * x402), each tagged live vs fallback. This is the same manifest the supervisor
 * feeds every Darwin's planner (so the genome reasons over real availability),
 * surfaced here for the operator. Deployment-wide + env-driven: identical for
 * every Darwin, so it lives on the fleet operator console rather than per-Darwin.
 */

import { useQuery } from "@tanstack/react-query";
import {
  Banknote,
  Fingerprint,
  Globe,
  PiggyBank,
  ShieldCheck,
  Wallet,
} from "lucide-react";
import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import type { MoneyCapabilities } from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";

function LiveBadge({ live }: { live: boolean }) {
  return (
    <Badge variant={live ? "default" : "outline"}>
      {live ? "live" : "fallback"}
    </Badge>
  );
}

function Rail({
  icon: Icon,
  label,
  provider,
  live,
  detail,
}: {
  icon: typeof Wallet;
  label: string;
  provider: string;
  live: boolean;
  detail?: string;
}) {
  return (
    <li className="flex items-center justify-between gap-3 px-4 py-2.5 text-sm">
      <div className="flex min-w-0 items-center gap-2">
        <Icon className="size-4 shrink-0 text-[var(--color-muted-foreground)]" />
        <span className="font-medium">{label}</span>
        <span className="mono truncate text-xs text-[var(--color-muted-foreground)]">
          {provider}
          {detail ? ` · ${detail}` : ""}
        </span>
      </div>
      <LiveBadge live={live} />
    </li>
  );
}

export function MoneyCapabilitiesCard() {
  const { data, isLoading, error } = useQuery<MoneyCapabilities>({
    queryKey: queryKeys.moneyCapabilities(),
    queryFn: () => api.moneyCapabilities(),
    staleTime: 5 * 60_000,
    retry: false,
  });

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
        <CardTitle className="flex items-center gap-2 text-base">
          <Banknote className="size-4 text-[var(--color-muted-foreground)]" />
          Money rails
        </CardTitle>
        {data ? (
          <Badge variant={data.mainnet ? "destructive" : "outline"}>
            {data.network}
            {data.mainnet ? " · real funds" : " · testnet"}
          </Badge>
        ) : null}
      </CardHeader>
      <CardContent className="p-0">
        {isLoading ? (
          <div className="p-4">
            <Skeleton className="h-40 w-full" />
          </div>
        ) : error ? (
          <p className="p-4 text-sm text-[var(--color-destructive)]">
            {error instanceof Error ? error.message : String(error)}
          </p>
        ) : !data ? null : (
          <ul className="divide-y divide-[var(--color-border)] border-t border-[var(--color-border)]">
            <Rail
              icon={Wallet}
              label="Wallet"
              provider={data.wallet.provider}
              live={data.wallet.live}
              detail={data.wallet.chains.join(", ")}
            />
            <Rail
              icon={Banknote}
              label="Fiat ramp"
              provider={data.fiat.provider}
              live={data.fiat.live}
            />
            <Rail
              icon={ShieldCheck}
              label="Sanctions"
              provider={data.sanctions.provider}
              live={data.sanctions.live}
            />
            <Rail
              icon={Fingerprint}
              label="KYC"
              provider={data.kyc.provider}
              live={data.kyc.live}
            />
            <Rail
              icon={PiggyBank}
              label="Omnibus funding"
              provider={data.omnibus.enabled ? "enabled" : "disabled"}
              live={data.omnibus.enabled}
            />
            <Rail
              icon={Globe}
              label="x402 HTTP payments"
              provider={data.x402.live ? "wired" : "off"}
              live={data.x402.live}
            />
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
