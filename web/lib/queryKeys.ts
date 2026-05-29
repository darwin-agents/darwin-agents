/**
 * Canonical TanStack Query keys for the dashboard. Centralising these makes
 * invalidation predictable (kill → invalidate everything under a darwin, etc.)
 * and matches docs/frontend/07-realtime-websockets-and-caching.md.
 */

export const queryKeys = {
  health: () => ["health"] as const,
  darwins: () => ["darwins"] as const,
  darwin: (id: string) => ["darwin", id] as const,
  events: (id: string, page = 0, filters?: Record<string, unknown>) =>
    ["darwin", id, "events", page, filters ?? {}] as const,
  ledger: (id: string, offset = 0) => ["darwin", id, "ledger", offset] as const,
  billing: (id: string) => ["darwin", id, "billing"] as const,
  controls: (id: string) => ["darwin", id, "controls"] as const,
  alerts: (id: string) => ["darwin", id, "alerts"] as const,
  escalations: (id: string) => ["darwin", id, "escalations"] as const,
  lineage: (id: string) => ["darwin", id, "lineage"] as const,
  // Payments — doc 17
  preapprovals: (id: string) => ["darwin", id, "preapprovals"] as const,
  freelancers: (ownerSubject: string) =>
    ["freelancers", ownerSubject] as const,
  myFreelancers: () => ["freelancers", "me"] as const,
};

export type QueryKey = ReturnType<(typeof queryKeys)[keyof typeof queryKeys]>;
