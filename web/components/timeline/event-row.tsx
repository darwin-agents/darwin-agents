"use client";

import { useState } from "react";
import {
  Activity,
  AlertTriangle,
  Brain,
  ChevronDown,
  ChevronRight,
  DollarSign,
  Dna,
  Flag,
  Shield,
  Skull,
  Sparkles,
  User,
  Workflow,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { formatTimestamp, formatUsdc } from "@/lib/format";
import type { DarwinEvent } from "@/lib/types";

const ICONS = {
  orchestrator: Brain,
  neuron_dispatch: Activity,
  llm_call: Sparkles,
  payment: DollarSign,
  escalation: Flag,
  mutation: Dna,
  reproduction: Workflow,
  owner_action: User,
  platform: Shield,
  death: Skull,
  unknown: AlertTriangle,
} as const;

type Category = keyof typeof ICONS;

function categoryOf(ev: DarwinEvent): Category {
  const raw = String(ev.category ?? ev.type ?? "").toLowerCase();
  if (raw in ICONS) return raw as Category;
  // Loose mappings from the runtime's actual event_type values.
  if (raw.includes("escalat")) return "escalation";
  if (raw.includes("payment") || raw.includes("ledger") || raw.includes("wallet")) return "payment";
  if (raw.includes("llm")) return "llm_call";
  if (raw.includes("dispatch") || raw.includes("neuron")) return "neuron_dispatch";
  if (raw.includes("mutat") || raw.includes("patch")) return "mutation";
  if (raw.includes("reproduc") || raw.includes("spawn")) return "reproduction";
  if (raw.includes("kill") || raw.includes("death")) return "death";
  if (raw.includes("owner") || raw.includes("control")) return "owner_action";
  if (raw.includes("quarantine") || raw.includes("throttle") || raw.includes("platform")) {
    return "platform";
  }
  if (raw.includes("orchestrat") || raw.includes("plan") || raw.includes("reflect")) {
    return "orchestrator";
  }
  return "unknown";
}

function summaryOf(ev: DarwinEvent, cat: Category): string {
  if (typeof ev.summary === "string" && ev.summary) return ev.summary;
  const type = String(ev.type ?? ev.category ?? cat);
  const neuron = typeof ev.neuron_id === "string" ? ev.neuron_id : null;
  switch (cat) {
    case "neuron_dispatch":
      return neuron ? `${neuron}` : type;
    case "llm_call": {
      const tier = (ev.payload as Record<string, unknown> | undefined)?.tier;
      const model = (ev.payload as Record<string, unknown> | undefined)?.model;
      return [tier, model].filter(Boolean).join(" → ") || type;
    }
    case "payment": {
      const dir = (ev.payload as Record<string, unknown> | undefined)?.direction;
      const amount = (ev.payload as Record<string, unknown> | undefined)?.amount_usdc;
      return [dir, typeof amount === "number" ? formatUsdc(amount) : null]
        .filter(Boolean)
        .join(" ") || type;
    }
    default:
      return type;
  }
}

export function EventRow({ ev }: { ev: DarwinEvent }) {
  const [open, setOpen] = useState(false);
  const cat = categoryOf(ev);
  const Icon = ICONS[cat];
  const summary = summaryOf(ev, cat);
  const cost = typeof ev.cost_usdc === "number" ? ev.cost_usdc : null;

  return (
    <div className="border-b border-[var(--color-border)] last:border-b-0">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="grid w-full grid-cols-[auto_auto_minmax(0,1fr)_auto_auto] items-center gap-3 px-3 py-2 text-left transition-colors hover:bg-[var(--color-muted)]/40"
      >
        <span className="mono text-xs text-[var(--color-muted-foreground)]">
          {formatTimestamp(ev.ts)}
        </span>
        <Icon
          aria-hidden
          className={cn("size-4", cat === "death" ? "text-[var(--color-destructive)]" : "text-[var(--color-muted-foreground)]")}
        />
        <span className="min-w-0">
          <span className="truncate text-sm">{summary}</span>
          {ev.neuron_id && (
            <span className="mono ml-2 text-xs text-[var(--color-muted-foreground)]">
              {String(ev.neuron_id)}
            </span>
          )}
        </span>
        <span className="w-20 text-right text-xs tabular-nums">
          {cost !== null ? formatUsdc(cost) : ""}
        </span>
        {open ? (
          <ChevronDown className="size-4 text-[var(--color-muted-foreground)]" />
        ) : (
          <ChevronRight className="size-4 text-[var(--color-muted-foreground)]" />
        )}
      </button>
      {open && (
        <div className="border-t border-[var(--color-border)] bg-[var(--color-background)] px-3 py-3 text-xs">
          <div className="mono mb-2 text-[var(--color-muted-foreground)]">
            event_id: {ev.event_id}
          </div>
          <pre className="mono max-h-72 overflow-auto whitespace-pre-wrap break-all rounded bg-[var(--color-card)] p-3 text-[11px] leading-relaxed">
            {JSON.stringify(ev, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}
