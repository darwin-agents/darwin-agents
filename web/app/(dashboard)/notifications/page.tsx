"use client";

/**
 * Notifications — Web Push opt-in + registered-device management (doc 22).
 *
 * The page asks the browser for permission, calls `pushManager.subscribe`
 * with the platform's VAPID public key, posts the resulting subscription
 * to `/me/push-subscriptions`, and lists what's already registered.
 *
 * Endpoint URLs are sensitive (endpoint + VAPID key = anyone can push to
 * that browser), so the list view only shows scheme://host. The full URL
 * lives in the browser's PushManager + the server-side store.
 */
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, BellOff, ShieldOff } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import {
  ensurePushSubscription,
  getCurrentSubscription,
  getPushPermissionState,
  isPushSupported,
  unsubscribePush,
  type PushPermissionState,
} from "@/lib/push";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

export default function NotificationsPage() {
  const qc = useQueryClient();
  const [perm, setPerm] = useState<PushPermissionState>("default");
  const [supported, setSupported] = useState<boolean>(true);

  // Read permission state on mount — it's synchronous, but only on the
  // client (Notification.permission throws during SSR).
  useEffect(() => {
    setSupported(isPushSupported());
    setPerm(getPushPermissionState());
  }, []);

  const vapid = useQuery({
    queryKey: queryKeys.pushVapidKey(),
    queryFn: () => api.getPushVapidPublicKey(),
    retry: false,
  });

  const subs = useQuery({
    queryKey: queryKeys.pushSubscriptions(),
    queryFn: () => api.listPushSubscriptions(),
  });

  const enable = useMutation({
    mutationFn: async () => {
      if (!vapid.data) throw new Error("VAPID public key not available");
      const sub = await ensurePushSubscription(vapid.data.public_key);
      if (!sub) throw new Error("Permission denied");
      return api.registerPushSubscription({
        endpoint: sub.endpoint,
        p256dh: sub.p256dh,
        auth: sub.auth,
        user_agent:
          typeof navigator !== "undefined"
            ? navigator.userAgent.slice(0, 200)
            : "",
      });
    },
    onSuccess: () => {
      setPerm(getPushPermissionState());
      void qc.invalidateQueries({ queryKey: queryKeys.pushSubscriptions() });
    },
  });

  const disable = useMutation({
    mutationFn: async () => {
      const endpoint = await unsubscribePush();
      if (!endpoint) return { revoked: false };
      return api.revokePushSubscription({ endpoint });
    },
    onSuccess: () => {
      setPerm(getPushPermissionState());
      void qc.invalidateQueries({ queryKey: queryKeys.pushSubscriptions() });
    },
  });

  const browserSub = useQuery({
    queryKey: ["push", "browser-subscription"] as const,
    queryFn: () => getCurrentSubscription(),
    enabled: supported,
  });

  const browserSubscribed = Boolean(browserSub.data);
  const serverConfigError =
    vapid.error instanceof ApiError && vapid.error.status === 503;

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Push notifications</CardTitle>
          <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
            Get notified the moment a Darwin needs you. Works on phones once
            you install the app to your home screen.
          </p>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {!supported ? (
            <Banner
              icon={ShieldOff}
              tone="muted"
              text="This browser doesn't support Web Push. Try Chrome, Edge, Firefox, or Safari on iOS 16.4+."
            />
          ) : serverConfigError ? (
            <Banner
              icon={ShieldOff}
              tone="muted"
              text="The platform wasn't started with Web Push configured. Set DARWIN_VAPID_PRIVATE_KEY + DARWIN_VAPID_SUBJECT and restart darwin-platform serve."
            />
          ) : perm === "denied" ? (
            <Banner
              icon={BellOff}
              tone="warn"
              text="You blocked notifications for this site. Re-enable from the browser's site settings, then come back."
            />
          ) : browserSubscribed ? (
            <>
              <Banner
                icon={Bell}
                tone="ok"
                text="Notifications are on for this device."
              />
              <Button
                onClick={() => disable.mutate()}
                disabled={disable.isPending}
                variant="outline"
              >
                {disable.isPending ? "Disabling…" : "Disable on this device"}
              </Button>
            </>
          ) : (
            <>
              <p className="text-sm text-[var(--color-muted-foreground)]">
                Tap below to subscribe this device. Your browser will ask for
                permission once.
              </p>
              <Button
                onClick={() => enable.mutate()}
                disabled={enable.isPending || !vapid.data}
              >
                <Bell className="mr-1 size-4" />
                {enable.isPending ? "Enabling…" : "Enable notifications"}
              </Button>
              {enable.error && (
                <p className="text-xs text-[var(--color-destructive)]">
                  {(enable.error as Error).message}
                </p>
              )}
            </>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Registered devices</CardTitle>
          <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
            Every device signed in to your account that has notifications on.
          </p>
        </CardHeader>
        <CardContent className="p-0">
          {subs.isLoading ? (
            <div className="space-y-2 p-4">
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
            </div>
          ) : !subs.data || subs.data.length === 0 ? (
            <p className="px-4 py-6 text-center text-sm text-[var(--color-muted-foreground)]">
              No devices registered.
            </p>
          ) : (
            <ul className="divide-y divide-[var(--color-border)]">
              {subs.data.map((s) => (
                <li
                  key={`${s.endpoint_origin}-${s.registered_at}`}
                  className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 px-4 py-2 text-sm"
                >
                  <div className="min-w-0">
                    <div className="mono truncate">{s.endpoint_origin}</div>
                    <div className="truncate text-xs text-[var(--color-muted-foreground)]">
                      {s.user_agent || "Unknown user agent"}
                    </div>
                  </div>
                  <div className="text-xs text-[var(--color-muted-foreground)]">
                    {new Date(s.registered_at * 1000).toLocaleDateString()}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function Banner({
  icon: Icon,
  tone,
  text,
}: {
  icon: React.ComponentType<{ className?: string }>;
  tone: "ok" | "warn" | "muted";
  text: string;
}) {
  const cls =
    tone === "ok"
      ? "border-[var(--color-status-alive)]/40 bg-[var(--color-status-alive)]/10 text-[var(--color-foreground)]"
      : tone === "warn"
        ? "border-[var(--color-alert-warning)]/40 bg-[var(--color-alert-warning)]/10 text-[var(--color-foreground)]"
        : "border-[var(--color-border)] bg-[var(--color-card)] text-[var(--color-muted-foreground)]";
  return (
    <div
      className={`flex items-start gap-2 rounded-md border px-3 py-2 text-sm ${cls}`}
    >
      <Icon className="mt-0.5 size-4 shrink-0" />
      <span>{text}</span>
    </div>
  );
}
