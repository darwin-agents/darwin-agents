"use client";

import { useState, type KeyboardEvent } from "react";
import { X, Plus } from "lucide-react";
import { cn } from "@/lib/cn";

/**
 * Multi-select editor that renders selected values as removable chips and
 * surfaces a candidate set as click-to-add suggestions. Free-text add via
 * the input handles values we haven't seen yet (no registry endpoint).
 */
export function ChipMultiSelect({
  value,
  onChange,
  candidates,
  placeholder,
  disabled,
  className,
}: {
  value: string[];
  onChange: (next: string[]) => void;
  candidates: string[];
  placeholder?: string;
  disabled?: boolean;
  className?: string;
}) {
  const [draft, setDraft] = useState("");

  const add = (raw: string) => {
    const v = raw.trim();
    if (!v) return;
    if (value.includes(v)) return;
    onChange([...value, v]);
  };

  const remove = (v: string) => {
    onChange(value.filter((x) => x !== v));
  };

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      add(draft);
      setDraft("");
    } else if (e.key === "Backspace" && draft === "" && value.length > 0) {
      remove(value[value.length - 1]!);
    }
  };

  const unselected = candidates.filter((c) => !value.includes(c)).slice(0, 12);

  return (
    <div className={cn("space-y-2", className)}>
      <div className="flex flex-wrap items-center gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-background)] p-2">
        {value.map((v) => (
          <span
            key={v}
            className="mono inline-flex items-center gap-1 rounded-full bg-[var(--color-accent)]/20 px-2 py-0.5 text-xs"
          >
            {v}
            <button
              type="button"
              disabled={disabled}
              onClick={() => remove(v)}
              className="rounded-full p-0.5 text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]"
              aria-label={`Remove ${v}`}
            >
              <X className="size-3" />
            </button>
          </span>
        ))}
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKey}
          onBlur={() => {
            if (draft.trim()) {
              add(draft);
              setDraft("");
            }
          }}
          placeholder={value.length === 0 ? (placeholder ?? "Type and press Enter") : ""}
          disabled={disabled}
          className="mono flex-1 min-w-[10ch] bg-transparent text-xs outline-none placeholder:text-[var(--color-muted-foreground)]"
        />
      </div>
      {unselected.length > 0 && (
        <div className="flex flex-wrap gap-1">
          <span className="text-[11px] uppercase tracking-wide text-[var(--color-muted-foreground)]">
            Suggestions
          </span>
          {unselected.map((c) => (
            <button
              key={c}
              type="button"
              disabled={disabled}
              onClick={() => add(c)}
              className="mono inline-flex items-center gap-1 rounded-full border border-[var(--color-border)] bg-[var(--color-card)] px-2 py-0.5 text-xs text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]"
            >
              <Plus className="size-3" />
              {c}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
