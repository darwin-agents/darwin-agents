import { cn } from "@/lib/cn";
import type { AlertEntry } from "@/lib/types";

const LEVEL_COLOR: Record<number, string> = {
  1: "bg-[var(--color-alert-warning)]/25 text-[var(--color-alert-warning)] border-[var(--color-alert-warning)]/40",
  2: "bg-[var(--color-alert-throttle)]/25 text-[var(--color-alert-throttle)] border-[var(--color-alert-throttle)]/40",
  3: "bg-[var(--color-alert-pause)]/25 text-[var(--color-alert-pause)] border-[var(--color-alert-pause)]/40",
  4: "bg-[var(--color-alert-quarantine)]/25 text-[var(--color-alert-quarantine)] border-[var(--color-alert-quarantine)]/40",
};

const LEVEL_LABEL: Record<number, string> = {
  1: "warn",
  2: "throttle",
  3: "pause",
  4: "quarantine",
};

export function AlertBadges({
  alerts,
  className,
}: {
  alerts: AlertEntry[] | undefined;
  className?: string;
}) {
  if (!alerts || alerts.length === 0) return null;
  // The newest alert per kind, at most three — not the whole history.
  const latest = [
    ...new Map([...alerts].sort((a, b) => a.ts - b.ts).map((a) => [a.kind, a])).values(),
  ]
    .reverse()
    .slice(0, 3);
  return (
    <span className={cn("flex flex-wrap gap-1", className)}>
      {latest.map((a) => {
        const level = Math.min(Math.max(a.suggested_level, 1), 4);
        return (
          <span
            key={a.kind}
            title={`${a.severity}: ${a.kind} (suggested level ${a.suggested_level})`}
            className={cn(
              "rounded-full border px-1.5 py-0.5 text-[10px] uppercase",
              LEVEL_COLOR[level],
            )}
          >
            {a.kind || LEVEL_LABEL[level]}
          </span>
        );
      })}
    </span>
  );
}
