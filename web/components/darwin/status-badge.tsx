import { cn } from "@/lib/cn";
import type { Lifecycle } from "@/lib/types";

const STATUS_MAP: Record<
  Lifecycle,
  { label: string; dot: string; text: string; bg: string; pulse?: boolean }
> = {
  alive: {
    label: "alive",
    dot: "bg-[var(--color-status-alive)]",
    text: "text-[var(--color-status-alive)]",
    bg: "bg-[var(--color-status-alive-soft)]/40",
    pulse: true,
  },
  paused: {
    label: "paused",
    dot: "bg-[var(--color-status-paused)]",
    text: "text-[var(--color-status-paused)]",
    bg: "bg-[var(--color-status-paused-soft)]/40",
  },
  quarantined: {
    label: "quarantined",
    dot: "bg-[var(--color-status-quarantined)]",
    text: "text-[var(--color-status-quarantined)]",
    bg: "bg-[var(--color-status-quarantined-soft)]/40",
  },
  dead: {
    label: "dead",
    dot: "bg-[var(--color-status-dead)]",
    text: "text-[var(--color-status-dead)]",
    bg: "bg-[var(--color-status-dead-soft)]/40",
  },
  killed: {
    label: "killed",
    dot: "bg-[var(--color-status-killed)]",
    text: "text-[var(--color-status-killed)] line-through",
    bg: "bg-[var(--color-status-killed-soft)]/40",
  },
};

function normalize(value: string): Lifecycle {
  const v = value.toLowerCase();
  if (v === "alive" || v === "paused" || v === "quarantined" || v === "dead" || v === "killed") {
    return v;
  }
  // Backend uses additional "alive-ish" intermediate states (pausing, killing).
  if (v.startsWith("pausing")) return "paused";
  if (v.startsWith("killing")) return "killed";
  return "alive";
}

export function StatusBadge({
  status,
  className,
}: {
  status: string | Lifecycle | null | undefined;
  className?: string;
}) {
  const resolved = status ? normalize(String(status)) : "alive";
  const spec = STATUS_MAP[resolved];
  return (
    <span
      aria-label={`status: ${spec.label}`}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border border-[var(--color-border)] px-2 py-0.5 text-xs font-medium",
        spec.bg,
        spec.text,
        className,
      )}
    >
      <span
        aria-hidden
        className={cn("inline-block size-2 rounded-full", spec.dot, spec.pulse && "darwin-pulse")}
      />
      {spec.label}
    </span>
  );
}
