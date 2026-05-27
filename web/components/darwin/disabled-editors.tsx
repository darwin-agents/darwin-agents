"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { ChipMultiSelect } from "@/components/ui/chip-multiselect";
import { Button } from "@/components/ui/button";

/**
 * Disabled-neurons / disabled-categories editor. The platform has no neuron
 * registry endpoint, so candidates are sourced from recent event records
 * (`neuron_id` and `category`). Operators can also free-type unseen values.
 */
export function DisabledEditors({
  darwinId,
  initialNeurons,
  initialCategories,
}: {
  darwinId: string;
  initialNeurons: string[];
  initialCategories: string[];
}) {
  const queryClient = useQueryClient();
  const [neurons, setNeurons] = useState(initialNeurons);
  const [categories, setCategories] = useState(initialCategories);

  // Sample recent events for candidate values. 100 rows is enough to surface
  // the active neurons without becoming a perf footgun.
  const events = useQuery({
    queryKey: queryKeys.events(darwinId, 0),
    queryFn: () => api.events(darwinId, { limit: 100, offset: 0 }),
  });

  const { neuronCandidates, categoryCandidates } = useMemo(() => {
    const n = new Set<string>();
    const c = new Set<string>();
    for (const ev of events.data?.events ?? []) {
      if (typeof ev.neuron_id === "string" && ev.neuron_id) n.add(ev.neuron_id);
      if (typeof ev.category === "string" && ev.category) c.add(ev.category);
    }
    for (const v of initialNeurons) n.add(v);
    for (const v of initialCategories) c.add(v);
    return {
      neuronCandidates: Array.from(n).sort(),
      categoryCandidates: Array.from(c).sort(),
    };
  }, [events.data, initialNeurons, initialCategories]);

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: queryKeys.controls(darwinId) });

  const saveNeurons = useMutation({
    mutationFn: (next: string[]) => api.setDisabledNeurons(darwinId, next),
    onSuccess: invalidate,
  });
  const saveCategories = useMutation({
    mutationFn: (next: string[]) => api.setDisabledCategories(darwinId, next),
    onSuccess: invalidate,
  });

  const neuronsDirty = !sameSet(neurons, initialNeurons);
  const categoriesDirty = !sameSet(categories, initialCategories);

  return (
    <div className="space-y-6">
      <Section
        title="Disabled neurons"
        description="The orchestrator will not dispatch to these. Re-enable by removing the chip."
        dirty={neuronsDirty}
        saving={saveNeurons.isPending}
        onSave={() => saveNeurons.mutate(neurons)}
        onReset={() => setNeurons(initialNeurons)}
        error={saveNeurons.error}
      >
        <ChipMultiSelect
          value={neurons}
          onChange={setNeurons}
          candidates={neuronCandidates}
          placeholder="e.g. content.publisher"
          disabled={saveNeurons.isPending}
        />
      </Section>

      <Section
        title="Disabled categories"
        description="Disable an entire family at once (e.g. all browser.* neurons)."
        dirty={categoriesDirty}
        saving={saveCategories.isPending}
        onSave={() => saveCategories.mutate(categories)}
        onReset={() => setCategories(initialCategories)}
        error={saveCategories.error}
      >
        <ChipMultiSelect
          value={categories}
          onChange={setCategories}
          candidates={categoryCandidates}
          placeholder="e.g. payments"
          disabled={saveCategories.isPending}
        />
      </Section>
    </div>
  );
}

function Section({
  title,
  description,
  children,
  dirty,
  saving,
  onSave,
  onReset,
  error,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
  dirty: boolean;
  saving: boolean;
  onSave: () => void;
  onReset: () => void;
  error: unknown;
}) {
  return (
    <div className="space-y-2 rounded-md border border-[var(--color-border)] p-3">
      <div className="flex items-start justify-between">
        <div>
          <div className="font-medium">{title}</div>
          <p className="text-xs text-[var(--color-muted-foreground)]">{description}</p>
        </div>
        <div className="flex gap-2">
          {dirty && (
            <Button variant="ghost" size="sm" onClick={onReset} disabled={saving}>
              Reset
            </Button>
          )}
          <Button size="sm" disabled={!dirty || saving} onClick={onSave}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </div>
      </div>
      {children}
      {error instanceof Error && (
        <p className="text-xs text-[var(--color-destructive)]">{error.message}</p>
      )}
    </div>
  );
}

function sameSet(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const s = new Set(a);
  return b.every((x) => s.has(x));
}
