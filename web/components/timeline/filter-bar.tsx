"use client";

/**
 * Timeline filter bar — text inputs for kind / neuron / tier whose values
 * are persisted in the URL so any view can be deep-linked. Pushes back
 * via `router.replace()` (no history entry per keystroke) and debounces
 * keystrokes so we don't fire a backend query per character.
 */
import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { X } from "lucide-react";

const TIERS = ["fast", "reasoning", "coding", "embedding", "vision"] as const;
const DEBOUNCE_MS = 350;

export interface TimelineFilters {
  kind: string;
  neuron: string;
  tier: string;
}

export function parseFilters(sp: URLSearchParams): TimelineFilters {
  return {
    kind: sp.get("kind") ?? "",
    neuron: sp.get("neuron") ?? "",
    tier: sp.get("tier") ?? "",
  };
}

export function TimelineFilterBar() {
  const router = useRouter();
  const pathname = usePathname();
  const sp = useSearchParams();
  const initial = parseFilters(new URLSearchParams(sp.toString()));
  const [kind, setKind] = useState(initial.kind);
  const [neuron, setNeuron] = useState(initial.neuron);
  const [tier, setTier] = useState(initial.tier);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      const next = new URLSearchParams();
      if (kind.trim()) next.set("kind", kind.trim());
      if (neuron.trim()) next.set("neuron", neuron.trim());
      if (tier.trim()) next.set("tier", tier.trim());
      const qs = next.toString();
      router.replace(`${pathname}${qs ? `?${qs}` : ""}` as never);
    }, DEBOUNCE_MS);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, neuron, tier]);

  const clear = () => {
    setKind("");
    setNeuron("");
    setTier("");
  };

  const hasAny = kind || neuron || tier;

  return (
    <div className="flex flex-wrap items-end gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-2">
      <Field label="Kind prefix" hint="e.g. event:payment">
        <input
          value={kind}
          onChange={(e) => setKind(e.target.value)}
          placeholder="event: / intent: / outcome"
          className={inputCls}
        />
      </Field>
      <Field label="Neuron" hint="exact match">
        <input
          value={neuron}
          onChange={(e) => setNeuron(e.target.value)}
          placeholder="content.publisher"
          className={inputCls}
        />
      </Field>
      <Field label="LLM tier">
        <select
          value={tier}
          onChange={(e) => setTier(e.target.value)}
          className={inputCls}
        >
          <option value="">any</option>
          {TIERS.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </Field>
      {hasAny ? (
        <button
          type="button"
          onClick={clear}
          className="ml-auto inline-flex items-center gap-1 rounded-md border border-[var(--color-border)] px-2 py-1 text-xs text-[var(--color-muted-foreground)] hover:bg-[var(--color-muted)]"
        >
          <X className="size-3" />
          Clear
        </button>
      ) : null}
    </div>
  );
}

const inputCls =
  "w-44 rounded-md border border-[var(--color-border)] bg-[var(--color-background)] px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]";

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1 text-[10px] uppercase tracking-wide text-[var(--color-muted-foreground)]">
      <span>
        {label}
        {hint && (
          <span className="ml-1 normal-case tracking-normal text-[var(--color-muted-foreground)]/70">
            ({hint})
          </span>
        )}
      </span>
      {children}
    </label>
  );
}
