"use client";

import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";

export function ApiBanner() {
  const { error, isError } = useQuery({
    queryKey: queryKeys.health(),
    // Probe /readyz, not /healthz: an ingress in front of the platform
    // (e.g. ingress-nginx) may reserve /healthz for its own controller
    // liveness and answer it itself, so /healthz never reaches the
    // supervisor (and loses its CORS headers). /readyz forwards normally
    // and is the better "ready to serve" signal for this banner anyway.
    queryFn: () => api.readyz(),
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
    retry: false,
  });

  if (!isError) return null;

  // Suppress for auth errors — the dev-auth gate handles those.
  if (error instanceof ApiError && (error.isAuth || error.isForbidden)) return null;

  return (
    <div className="border-b border-[var(--color-border)] bg-[var(--color-destructive)]/10 px-4 py-2 text-xs text-[var(--color-destructive)]">
      Platform unreachable — showing stale data. Reading-only state until connection restored.
    </div>
  );
}
