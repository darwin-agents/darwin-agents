/**
 * Response DTOs the UI consumes.
 *
 * Why this file isn't generated: the platform's FastAPI handlers return
 * `dict[str, Any]`, so the OpenAPI schema marks every response as `unknown`.
 * `lib/api.generated.ts` (refreshed via `pnpm codegen`) covers request bodies
 * and path/query parameters — we re-export those below. Response shapes live
 * here, mirroring the dataclasses in `src/darwin_platform/`.
 */
import type { components } from "./api.generated";

/* Generated request bodies — typed against the live schema. */
export type SpawnRequest = components["schemas"]["SpawnRequest"];
export type KillRequest = components["schemas"]["KillRequest"];
export type PauseRequest = components["schemas"]["PauseRequest"];
export type SpendLockRequest = components["schemas"]["SpendLockRequest"];
export type ReproductionBlockRequest =
  components["schemas"]["ReproductionBlockRequest"];
export type DisabledNeuronsRequest = components["schemas"]["DisabledNeuronsRequest"];
export type DisabledCategoriesRequest =
  components["schemas"]["DisabledCategoriesRequest"];
export type EscalationResponseRequest =
  components["schemas"]["EscalationResponseRequest"];
export type HarvestRequest = components["schemas"]["HarvestRequest"];
export type HarvestRuleRequest = components["schemas"]["HarvestRuleRequest"];
export type VaultPutRequest = components["schemas"]["VaultPutRequest"];
export type FiatDepositQuoteRequest =
  components["schemas"]["FiatDepositQuoteRequest"];
export type FiatConfirmDepositRequest =
  components["schemas"]["FiatConfirmDepositRequest"];
export type FiatWithdrawRequest = components["schemas"]["FiatWithdrawRequest"];

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

/**
 * One hash-chained audit record. Field names mirror the runtime — `seq`
 * (monotonic int, canonical identity) and `kind` (event taxonomy string).
 * Legacy `event_id` / `category` / `type` are tolerated for older callsites
 * that haven't been migrated yet.
 */
export interface DarwinEvent {
  seq?: number;
  hash?: string;
  prev_hash?: string;
  ts: string | number;
  kind?: string;
  payload?: Record<string, unknown>;
  // Legacy compatibility — pre-codegen UI code used these names.
  event_id?: string;
  category?: string;
  type?: string;
  neuron_id?: string;
  summary?: string;
  cost_usdc?: number;
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

/**
 * Real shape from `RoutedTicket.to_dict()` in
 * `src/darwin_platform/escalations/escalation.py`. Timestamps are unix
 * seconds (floats), not ISO strings.
 */
export interface EscalationTicket {
  ticket_id: string;
  darwin_id: string;
  neuron?: string;
  ticket_class?: string;
  status?: "open" | "responded" | "expired" | string;
  opened_ts?: number;
  deadline_ts?: number;
  age_seconds?: number;
  remaining_seconds?: number;
  past_deadline?: boolean;
  tick?: number | null;
  detail?: Record<string, unknown>;
  response?: {
    decision: string;
    data: Record<string, unknown>;
    responded_ts: number;
  } | null;
  notification_sent_ts?: number | null;
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
