/**
 * Hand-maintained DTOs mirroring the FastAPI surface in
 * `src/darwin_platform/api.py`. These will be superseded by
 * `lib/api.generated.ts` once `pnpm codegen` runs against a live platform.
 * Keep this file narrow — only types the UI actually consumes.
 */

export type Lifecycle = "alive" | "paused" | "quarantined" | "dead" | "killed";

export interface DarwinStatus {
  darwin_id: string;
  lifecycle: Lifecycle;
  balance_usdc?: number;
  treasury_usdc?: number;
  generation?: number;
  parent_id?: string | null;
  spawned_at?: string;
  last_heartbeat_at?: string | null;
  cause_of_death?: string | null;
  // Loose-tail for fields the doc enumerates but the runtime adds opportunistically.
  [k: string]: unknown;
}

export interface DarwinListResponse {
  darwins: string[];
}

export interface DarwinEvent {
  event_id: string;
  ts: string;
  category?: string;
  type?: string;
  neuron_id?: string;
  summary?: string;
  cost_usdc?: number;
  payload?: Record<string, unknown>;
  [k: string]: unknown;
}

export interface EventsResponse {
  darwin_id: string;
  total: number;
  offset: number;
  limit: number;
  events: DarwinEvent[];
}

export interface LedgerEntry {
  seq: number;
  ts: string;
  type: string;
  direction: string;
  amount_usdc: number;
  counterparty?: string;
  category?: string;
  memo?: string;
  [k: string]: unknown;
}

export interface LedgerResponse {
  darwin_id: string;
  total: number;
  offset: number;
  limit: number;
  entries: LedgerEntry[];
}

export interface BillingResponse {
  darwin_id: string;
  balance_usdc?: number;
  burn_rate_usdc_per_hour?: number;
  llm_cost_usdc?: number;
  sandbox_cost_usdc?: number;
  browser_cost_usdc?: number;
  [k: string]: unknown;
}

export interface OwnerControls {
  darwin_id: string;
  spend_lock?: { locked: boolean; reason?: string };
  reproduction_block?: { blocked: boolean };
  disabled_neurons?: string[];
  disabled_categories?: string[];
  [k: string]: unknown;
}

export interface EscalationTicket {
  escalation_id: string;
  darwin_id: string;
  class?: string;
  klass?: string;
  summary?: string;
  status?: string;
  opened_at?: string;
  deadline_at?: string | null;
  evidence_uris?: string[];
  proposed_action?: Record<string, unknown>;
  [k: string]: unknown;
}

export interface EscalationsResponse {
  darwin_id: string;
  include_resolved: boolean;
  tickets: EscalationTicket[];
}

export interface LineageNode {
  darwin_id: string;
  generation?: number;
  lifecycle?: Lifecycle;
  parent_id?: string | null;
  children?: LineageNode[];
  fitness?: number | null;
  cause_of_death?: string | null;
  [k: string]: unknown;
}

export interface LineageTreeResponse {
  darwin_id: string;
  root?: LineageNode;
  ancestors?: LineageNode[];
  descendants?: LineageNode[];
  [k: string]: unknown;
}

export interface AlertEntry {
  level?: number;
  code?: string;
  summary?: string;
  ts?: string;
  [k: string]: unknown;
}

export interface AlertsResponse {
  darwin_id: string;
  alerts: AlertEntry[];
}
