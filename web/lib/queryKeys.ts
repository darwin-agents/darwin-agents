/**
 * Canonical TanStack Query keys for the dashboard. Centralising these makes
 * invalidation predictable (kill → invalidate everything under a darwin, etc.)
 * and matches docs/frontend/07-realtime-websockets-and-caching.md.
 */

export const queryKeys = {
  health: () => ["health"] as const,
  capabilities: () => ["platform", "capabilities"] as const,
  darwins: () => ["darwins"] as const,
  darwin: (id: string) => ["darwin", id] as const,
  events: (id: string, page = 0, filters?: Record<string, unknown>) =>
    ["darwin", id, "events", page, filters ?? {}] as const,
  ledger: (id: string, offset = 0) => ["darwin", id, "ledger", offset] as const,
  billing: (id: string) => ["darwin", id, "billing"] as const,
  ledgerSummary: (id: string, year?: number) =>
    ["darwin", id, "ledger-summary", year ?? "lifetime"] as const,
  organismTables: (id: string) => ["darwin", id, "organism", "tables"] as const,
  organismRows: (id: string, table: string, offset = 0) =>
    ["darwin", id, "organism", "rows", table, offset] as const,
  forensics: (id: string) => ["darwin", id, "forensics"] as const,
  forensicSnapshot: (id: string, snapshotId: string) =>
    ["darwin", id, "forensics", snapshotId] as const,
  controls: (id: string) => ["darwin", id, "controls"] as const,
  alerts: (id: string) => ["darwin", id, "alerts"] as const,
  escalations: (id: string) => ["darwin", id, "escalations"] as const,
  lineage: (id: string) => ["darwin", id, "lineage"] as const,
  // Payments — doc 17
  preapprovals: (id: string) => ["darwin", id, "preapprovals"] as const,
  freelancers: (ownerSubject: string) =>
    ["freelancers", ownerSubject] as const,
  myFreelancers: () => ["freelancers", "me"] as const,
  // Push subscriptions — doc 22 (PWA escalation companion)
  pushSubscriptions: () => ["push", "subscriptions"] as const,
  pushVapidKey: () => ["push", "vapid-key"] as const,
  // Proposals — neuron-mutation review
  proposals: (id: string) => ["darwin", id, "proposals"] as const,
  // Operator surfaces
  throttle: (id: string) => ["darwin", id, "throttle"] as const,
  resourceThrottle: (id: string) =>
    ["darwin", id, "resource-throttle"] as const,
  walletPolicy: (id: string) => ["darwin", id, "wallet-policy"] as const,
  stagingStatus: () => ["platform", "staging", "status"] as const,
  stagingHistory: (limit: number) =>
    ["platform", "staging", "history", limit] as const,
  signingKey: () => ["platform", "signing", "public-key"] as const,
  kycStatus: (subject: string) => ["kyc", "status", subject] as const,
};

export type QueryKey = ReturnType<(typeof queryKeys)[keyof typeof queryKeys]>;
