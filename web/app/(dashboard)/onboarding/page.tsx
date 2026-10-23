"use client";

import Link from "next/link";
import { useMutation, useQuery } from "@tanstack/react-query";
import { CheckCircle2, Circle } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

/** First run: verify identity → turn on notifications → spawn → fund. Each
 *  step reads the real state, so it's also where an owner sees what's left. */
export default function OnboardingPage() {
  const kyc = useQuery({
    queryKey: ["onboarding", "kyc"],
    queryFn: () => api.kycStatus(),
    retry: false,
  });
  const kycOff = kyc.error instanceof ApiError && kyc.error.status === 503;
  const kycDone = kycOff || kyc.data?.status === "approved";
  const start = useMutation({
    mutationFn: () => api.kycStart({}),
    onSuccess: (r) => window.open(r.redirect_url, "_blank", "noopener"),
  });

  const push = useQuery({
    queryKey: ["onboarding", "push"],
    queryFn: () => api.listPushSubscriptions(),
    retry: false,
  });
  const darwins = useQuery({
    queryKey: ["onboarding", "darwins"],
    queryFn: () => api.listDarwins(),
  });
  const first = darwins.data?.darwins[0];

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-4">
      <h1 className="text-xl font-semibold tracking-tight">Get started</h1>

      <Step done={kycDone} title="1. Verify your identity">
        {kycOff ? (
          <p>Not required on this platform.</p>
        ) : kycDone ? (
          <p>Verified.</p>
        ) : (
          <>
            <p>
              Spawning, funding and harvesting need a verified adult owner. Status:{" "}
              <b>{kyc.data?.status ?? "unknown"}</b>.
            </p>
            <div className="mt-2 flex gap-2">
              <Button size="sm" disabled={start.isPending} onClick={() => start.mutate()}>
                Verify identity
              </Button>
              <Button size="sm" variant="outline" onClick={() => kyc.refetch()}>
                I&apos;ve finished — check again
              </Button>
            </div>
          </>
        )}
      </Step>

      <Step done={(push.data?.length ?? 0) > 0} title="2. Turn on notifications">
        <p>
          Your Darwin asks you before risky steps (spending, captchas, identity checks).{" "}
          <Link href="/notifications" className="underline">
            Enable notifications
          </Link>{" "}
          on the device you&apos;ll answer from.
        </p>
      </Step>

      <Step done={Boolean(first)} title="3. Spawn a Darwin">
        <p>
          <Link href="/darwins/new" className="underline">
            Spawn one
          </Link>{" "}
          with a small opening budget.
        </p>
      </Step>

      <Step done={false} title="4. Fund it and set your payout address">
        {first ? (
          <p>
            On{" "}
            <Link href={{ pathname: `/darwins/${first}` }} className="underline">
              your Darwin
            </Link>
            , send USDC on Base to its address (or deposit by card), and set where harvests go.
          </p>
        ) : (
          <p>After spawning, fund it from its page.</p>
        )}
      </Step>
    </div>
  );
}

function Step({
  done,
  title,
  children,
}: {
  done: boolean;
  title: string;
  children: React.ReactNode;
}) {
  const Icon = done ? CheckCircle2 : Circle;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Icon
            className={`size-4 ${done ? "text-[var(--color-status-alive)]" : "text-[var(--color-muted-foreground)]"}`}
          />
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent className="text-sm text-[var(--color-muted-foreground)]">{children}</CardContent>
    </Card>
  );
}
