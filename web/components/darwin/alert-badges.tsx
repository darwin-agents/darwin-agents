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
  return (
    <span className={cn("flex flex-wrap gap-1", className)}>
      {alerts.map((a, i) => {
        const level = typeof a.level === "number" ? a.level : 1;
        const color = LEVEL_COLOR[level] ?? LEVEL_COLOR[1];
        const label = LEVEL_LABEL[level] ?? "alert";
        return (
          <span
            key={`${a.code ?? "alert"}-${i}`}
            title={a.summary ?? a.code ?? ""}
            className={cn("rounded-full border px-1.5 py-0.5 text-[10px] uppercase", color)}
          >
            {a.code ?? label}
          </span>
        );
      })}
    </span>
  );
}
