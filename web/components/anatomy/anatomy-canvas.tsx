"use client";

import { useMemo, useRef, useState } from "react";
import { cn } from "@/lib/cn";

// Center + ring fixed at module scope so the memo dependency lists stay
// stable; the canvas size doesn't change at runtime.
const CENTER = { x: 320, y: 240 } as const;
const RING_RADIUS = 200;

type NeuronStatus = "idle" | "active" | "disabled" | "error";

interface AnatomyNeuron {
  id: string;
  label: string;
  category: string;
  status: NeuronStatus;
  last_dispatch_at: number | null;
  last_error_at: number | null;
  dispatch_count_recent: number;
  version?: string | null;
}

interface AnatomyEdge {
  from: string;
  to: string;
  active: boolean;
  weight: number;
}

interface AnatomySnapshot {
  orchestrator: { id: string; label: string; state: string; tick: number | null };
  neurons: AnatomyNeuron[];
  edges: AnatomyEdge[];
  heatmap: Record<string, number>;
}

const STATUS_COLOR: Record<NeuronStatus, string> = {
  idle: "var(--color-muted-foreground)",
  active: "var(--color-status-alive)",
  disabled: "var(--color-status-dead)",
  error: "var(--color-destructive)",
};

const STATUS_FILL: Record<NeuronStatus, string> = {
  idle: "var(--color-card)",
  active: "var(--color-status-alive-soft)",
  disabled: "var(--color-status-dead-soft)",
  error: "var(--color-destructive)",
};

/**
 * SVG radial layout: orchestrator at center, neurons arranged on a ring
 * by category. Edges connect orchestrator → each neuron, with thickness
 * scaled by recent dispatch heat. React Flow would give pan/zoom/minimap
 * but adds ~120 KB; this stays under 10 KB for the M3-A acceptance bar.
 */
export function AnatomyCanvas({
  snapshot,
  selectedNeuronId,
  onSelectNeuron,
}: {
  snapshot: AnatomySnapshot;
  selectedNeuronId?: string | null;
  onSelectNeuron?: (id: string | null) => void;
}) {
  // Group neurons by category so the layout puts similar ones next to each
  // other (content.* together, browser.* together, etc.).
  const layout = useMemo(() => {
    const sorted = [...snapshot.neurons].sort((a, b) => {
      if (a.category !== b.category) return a.category.localeCompare(b.category);
      return a.id.localeCompare(b.id);
    });
    const count = sorted.length;
    return sorted.map((n, i) => {
      const angle = (i / Math.max(count, 1)) * Math.PI * 2 - Math.PI / 2;
      return {
        ...n,
        x: CENTER.x + Math.cos(angle) * RING_RADIUS,
        y: CENTER.y + Math.sin(angle) * RING_RADIUS,
      };
    });
  }, [snapshot.neurons]);

  const positions = useMemo(() => {
    const m = new Map<string, { x: number; y: number }>();
    for (const n of layout) m.set(n.id, { x: n.x, y: n.y });
    m.set(snapshot.orchestrator.id, CENTER);
    return m;
  }, [layout, snapshot.orchestrator.id]);

  const orchestratorState = snapshot.orchestrator.state;
  const orchActive = orchestratorState === "alive" || orchestratorState === "running";

  return (
    <svg viewBox="0 0 640 480" className="w-full" role="img" aria-label="Anatomy canvas">
      <defs>
        <radialGradient id="orchGlow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="var(--color-accent)" stopOpacity="0.4" />
          <stop offset="100%" stopColor="var(--color-accent)" stopOpacity="0" />
        </radialGradient>
      </defs>

      {/* Edges */}
      {snapshot.edges.map((e) => {
        const from = positions.get(e.from);
        const to = positions.get(e.to);
        if (!from || !to) return null;
        const stroke = e.active
          ? "var(--color-status-alive)"
          : "var(--color-border)";
        const strokeWidth = e.active ? 2 : 0.5 + e.weight * 2;
        return (
          <line
            key={`${e.from}->${e.to}`}
            x1={from.x}
            y1={from.y}
            x2={to.x}
            y2={to.y}
            stroke={stroke}
            strokeWidth={strokeWidth}
            strokeDasharray={e.active ? "4 2" : undefined}
            opacity={selectedNeuronId && selectedNeuronId !== e.to ? 0.2 : 1}
          >
            {e.active && (
              <animate
                attributeName="stroke-dashoffset"
                values="0;-6"
                dur="0.6s"
                repeatCount="indefinite"
              />
            )}
          </line>
        );
      })}

      {/* Orchestrator center node */}
      {orchActive && (
        <circle cx={CENTER.x} cy={CENTER.y} r="44" fill="url(#orchGlow)" />
      )}
      <circle
        cx={CENTER.x}
        cy={CENTER.y}
        r="28"
        fill="var(--color-card)"
        stroke="var(--color-accent)"
        strokeWidth="2"
      />
      <text
        x={CENTER.x}
        y={CENTER.y + 4}
        textAnchor="middle"
        className="fill-[var(--color-foreground)] text-[11px] font-medium"
      >
        orchestrator
      </text>

      {/* Neuron nodes */}
      {layout.map((n) => {
        const selected = selectedNeuronId === n.id;
        const dim =
          selectedNeuronId && !selected ? 0.35 : 1;
        return (
          <g
            key={n.id}
            transform={`translate(${n.x},${n.y})`}
            opacity={dim}
            tabIndex={0}
            role="button"
            aria-label={`Neuron ${n.label}, status ${n.status}`}
            onClick={() => onSelectNeuron?.(selected ? null : n.id)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onSelectNeuron?.(selected ? null : n.id);
              }
            }}
            style={{ cursor: "pointer" }}
          >
            <circle
              r={n.status === "active" ? 22 : 18}
              fill={STATUS_FILL[n.status]}
              stroke={STATUS_COLOR[n.status]}
              strokeWidth={selected ? 3 : 1.5}
              strokeDasharray={n.status === "disabled" ? "3 3" : undefined}
              className={n.status === "active" ? "darwin-pulse" : ""}
            />
            <text
              y={36}
              textAnchor="middle"
              className={cn(
                "mono fill-[var(--color-foreground)] text-[10px]",
                selected && "font-bold",
              )}
            >
              {n.id}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

/**
 * REST-only Anatomy viewer with auto-refresh. The WS variant in
 * components/anatomy/anatomy-live.tsx layers on top of this.
 */
export function useAnatomyPolling(_darwinId: string) {
  const ref = useRef<AnatomySnapshot | null>(null);
  const [, force] = useState(0);

  // Caller wires the data; we just keep stable identity.
  return {
    snapshot: ref.current,
    setSnapshot: (next: AnatomySnapshot) => {
      ref.current = next;
      force((x) => x + 1);
    },
  };
}

export type { AnatomySnapshot, AnatomyNeuron, AnatomyEdge };
