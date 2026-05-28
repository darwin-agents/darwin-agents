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

// Runtime emits canonical `kind` strings like `event:perceive`,
// `intent:neuron.content.writer`, `outcome`. Older paths used
// `category`/`type`; tolerate both.
function categoryOf(ev: DarwinEvent): Category {
  const raw = String(ev.kind ?? ev.category ?? ev.type ?? "").toLowerCase();
  if (raw in ICONS) return raw as Category;
  if (raw.startsWith("intent:neuron.") || raw === "outcome") return "neuron_dispatch";
  if (raw.includes("escalat")) return "escalation";
  if (raw.includes("ledger") || raw.includes("payment") || raw.includes("wallet") || raw.includes("treasury")) return "payment";
  if (raw.includes("llm")) return "llm_call";
  if (raw.includes("dispatch") || raw.includes("neuron")) return "neuron_dispatch";
  if (raw.includes("mutat") || raw.includes("patch") || raw.includes("amendment")) return "mutation";
  if (raw.includes("reproduc") || raw.includes("spawn") || raw.includes("birth")) return "reproduction";
  if (raw.includes("kill") || raw.includes("death")) return "death";
  if (raw.includes("owner") || raw.includes("control")) return "owner_action";
  if (raw.includes("quarantine") || raw.includes("throttle") || raw.includes("platform") || raw.includes("merkle")) {
    return "platform";
  }
  if (raw.startsWith("event:") || raw.includes("orchestrat") || raw.includes("plan") || raw.includes("perceive") || raw.includes("reflect") || raw.includes("check_reproduction")) {
    return "orchestrator";
  }
  return "unknown";
}

function summaryOf(ev: DarwinEvent, cat: Category): string {
  if (typeof ev.summary === "string" && ev.summary) return ev.summary;
  const kindRaw = String(ev.kind ?? ev.type ?? ev.category ?? cat);
  const label = kindRaw
    .replace(/^event:/, "")
    .replace(/^intent:neuron\./, "→ ")
    .replace(/^intent:/, "→ ");
  const payload = (ev.payload ?? {}) as Record<string, unknown>;
  switch (cat) {
    case "neuron_dispatch": {
      const status = typeof payload.status === "string" ? payload.status : null;
      const topic = typeof payload.topic === "string" ? payload.topic : null;
      const detail = payload.detail as Record<string, unknown> | undefined;
      const words = detail && typeof detail.words === "number" ? `${detail.words}w` : null;
      return [label, topic, status, words].filter(Boolean).join(" · ");
    }
    case "llm_call": {
      const tier = payload.tier;
      const model = payload.model;
      return [tier, model].filter(Boolean).join(" → ") || label;
    }
    case "payment": {
      const dir = payload.direction;
      const amount = payload.amount_usdc;
      const treasury = payload.treasury_usdc;
      const parts: (string | number | null)[] = [label];
      if (dir) parts.push(String(dir));
      if (typeof amount === "number") parts.push(formatUsdc(amount));
      if (typeof treasury === "number") parts.push(`bal ${formatUsdc(treasury)}`);
      return parts.filter(Boolean).join(" · ");
    }
    case "orchestrator": {
      const tick = typeof payload.tick === "number" ? `t${payload.tick}` : null;
      const action = typeof payload.action === "string" ? payload.action : null;
      const reason = typeof payload.reason === "string" ? payload.reason : null;
      const balance = typeof payload.balance_usdc === "number" ? formatUsdc(payload.balance_usdc) : null;
      return [label, tick, action, reason, balance].filter(Boolean).join(" · ");
    }
    default:
      return label;
  }
}

function costOf(ev: DarwinEvent): number | null {
  if (typeof ev.cost_usdc === "number") return ev.cost_usdc;
  const payload = (ev.payload ?? {}) as Record<string, unknown>;
  if (typeof payload.cost_usdc === "number") return payload.cost_usdc;
  const detail = payload.detail as Record<string, unknown> | undefined;
  if (detail && typeof detail.cost_usdc === "number") return detail.cost_usdc;
  return null;
}

export function EventRow({
  ev,
  open: controlledOpen,
  onToggle,
}: {
  ev: DarwinEvent;
  open?: boolean;
  onToggle?: () => void;
}) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const isControlled = controlledOpen !== undefined;
  const open = isControlled ? controlledOpen : uncontrolledOpen;
  const toggle = onToggle ?? (() => setUncontrolledOpen((o) => !o));
  const cat = categoryOf(ev);
  const Icon = ICONS[cat];
  const summary = summaryOf(ev, cat);
  const cost = costOf(ev);

  return (
    <div className="border-b border-[var(--color-border)] last:border-b-0">
      <button
        type="button"
        onClick={toggle}
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
            seq: {ev.seq ?? "—"} · kind: {ev.kind ?? ev.type ?? ev.category ?? "—"}
            {ev.hash && <> · hash: {String(ev.hash).slice(0, 16)}…</>}
          </div>
          <pre className="mono max-h-72 overflow-auto whitespace-pre-wrap break-all rounded bg-[var(--color-card)] p-3 text-[11px] leading-relaxed">
            {JSON.stringify(ev, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}
