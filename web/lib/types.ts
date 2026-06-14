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
  // Epoch seconds from the platform (formatTimestamp handles both).
  ts: string | number;
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

/**
 * Jurisdiction-neutral accounting summary (docs 05, 17) from
 * `payments.ledger_export.movements_to_summary`. NOT a statutory tax form —
 * the owner hands this to their accountant. Amounts are USDC.
 */
export interface LedgerSummaryCategory {
  direction: string;
  count: number;
  amount_usdc: number;
}

export interface LedgerSummary {
  currency: string;
  period: {
    year: number | null;
    start_utc: string | null;
    end_utc: string | null;
  };
  movement_count: number;
  total_in_usdc: number;
  total_out_usdc: number;
  net_usdc: number;
  by_category: Record<string, LedgerSummaryCategory>;
  /** Present only for a dated period: movements with no timestamp excluded. */
  undated_excluded?: number;
  [k: string]: unknown;
}

/**
 * Post-mortem soma forensics (doc 05). The index is metadata-only
 * (`list_soma_forensics`); file contents arrive via the per-snapshot read.
 */
export interface ForensicSnapshotSummary {
  snapshot_id: string;
  darwin_id?: string | null;
  reason?: string | null;
  captured_at?: number | null;
  file_count?: number | null;
  total_bytes?: number | null;
  soma_digest?: string | null;
  metadata?: Record<string, unknown>;
  [k: string]: unknown;
}

export interface ForensicSnapshotListResponse {
  snapshots: ForensicSnapshotSummary[];
}

/**
 * Full death-time soma manifest. `contents` maps soma-relative path -> file
 * text (null for a non-UTF-8/binary file); k8s deaths instead carry a signed
 * `layers` fingerprint (no file bodies). Both surface through the same route.
 */
export interface ForensicManifest {
  schema?: string;
  snapshot_id?: string;
  darwin_id?: string;
  reason?: string;
  captured_at?: number;
  soma_root?: string;
  file_count?: number;
  total_bytes?: number;
  soma_digest?: string;
  mutation_snapshot_ids?: string[];
  metadata?: Record<string, unknown>;
  files?: Record<string, { sha256: string; bytes: number; binary: boolean }>;
  contents?: Record<string, string | null>;
  layers?: Array<Record<string, unknown>>;
  identity?: Record<string, unknown>;
  [k: string]: unknown;
}

/**
 * Deployment capability flags (`GET /platform/capabilities`) the dashboard
 * gates its UI on. The `*_browser` / `*_viewer` flags fold in BOTH the
 * deployment config and the caller's operator scope, so a tab is shown only
 * when the current user can actually use it here. The raw `tenant_namespaces`
 * / `soma_forensics` / `k8s` booleans are informational (deployment facts).
 */
export interface PlatformCapabilities {
  tenant_namespaces: boolean;
  soma_forensics: boolean;
  k8s: boolean;
  organism_browser: boolean;
  forensics_viewer: boolean;
  // Raw operator scope of the caller. Gates the operator-only consoles
  // (pre-approvals, payments, fiat withdraw) so an owner token never sees a
  // tab that would only ever 403.
  is_operator: boolean;
  [k: string]: unknown;
}

/**
 * Which money rails this deployment actually has wired, from
 * `GET /platform/money/capabilities`. `live` distinguishes a real adapter from
 * a stub/offline fallback (env may be set but the adapter still fake). Same
 * manifest the supervisor feeds each Darwin's planner. Read-only / informational.
 */
export interface MoneyCapabilities {
  network: string; // e.g. "base-mainnet" | "base-sepolia"
  mainnet: boolean;
  wallet: { provider: string; live: boolean; chains: string[] };
  fiat: { provider: string; live: boolean };
  sanctions: { provider: string; live: boolean };
  kyc: { provider: string; live: boolean };
  omnibus: { enabled: boolean };
  x402: { live: boolean };
  // Operator scope of the caller (folded in server-side, like PlatformCapabilities).
  is_operator: boolean;
  [k: string]: unknown;
}

/**
 * Operator data browser onto a Darwin's organism (business) database, read
 * through the SELECT-only dashboard role (`adapters.organism_reader`).
 */
export interface OrganismTablesResponse {
  darwin_id: string;
  tables: string[];
}

export interface OrganismRowsResponse {
  darwin_id: string;
  table: string;
  columns: string[];
  rows: Array<Record<string, unknown>>;
  limit: number;
  offset: number;
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

/**
 * Real shape from `Supervisor.get_owner_controls` — `spend_lock` is the
 * wallet's `{spend_locked, reason}` dict and `reproduction_blocked` is a
 * flat boolean (there is no nested `reproduction_block` object).
 */
export interface OwnerControls {
  spend_lock?: { spend_locked: boolean; reason?: string | null };
  reproduction_blocked?: boolean;
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

/* ---------------------------------------------------------------------------
 * Payments — doc 17 (preapprovals, freelancer registry, fiat payouts)
 * ------------------------------------------------------------------------- */

export interface PreApprovalRule {
  rule_id: string;
  max_amount_usdc: number;
  counterparty_category?: string | null;
  counterparty_substring?: string | null;
  expires_at?: number | null;
  max_uses?: number | null;
  used_count: number;
}

export interface PreApprovalRequest {
  max_amount_usdc: number;
  counterparty_category?: string | null;
  counterparty_substring?: string | null;
  expires_at?: number | null;
  max_uses?: number | null;
}

export interface FreelancerDestination {
  owner_subject: string;
  freelancer_id: string;
  fiat_destination: string;
  display_name?: string;
  kyc_status?: "unverified" | "pending" | "verified" | string;
  registered_at?: number;
  metadata?: Record<string, unknown>;
}

export interface RegisterFreelancerRequest {
  owner_subject: string;
  freelancer_id: string;
  fiat_destination: string;
  display_name?: string;
  kyc_status?: string;
  metadata?: Record<string, unknown> | null;
}

export interface DarwinFiatPayoutRequest {
  amount_usdc: number;
  freelancer_id: string;
  escalation_id?: string | null;
}

export interface DarwinFiatRefundRequest {
  amount_usdc: number;
  reason?: string;
}

export interface RetryOmnibusFundRequest {
  amount_usdc: number;
}

/** OmnibusEntry as returned by routes that surface ledger rows. */
export interface OmnibusEntryDTO {
  kind: string;
  amount_usdc: number;
  counterparty: string;
  actor: string;
  ts: number;
  memo?: string;
  tx_hash?: string;
  metadata?: Record<string, unknown>;
}

/* -------------------------------------------------------------------------
 * Push subscriptions (doc 22 — PWA escalation companion)
 * --------------------------------------------------------------------- */

export interface VapidPublicKeyResponse {
  /** Base64url-encoded P-256 public key the PWA hands to
   *  `pushManager.subscribe({ applicationServerKey })`. */
  public_key: string;
}

export interface PushSubscriptionRequest {
  endpoint: string;
  p256dh: string;
  auth: string;
  user_agent?: string;
}

export interface PushSubscriptionRow {
  /** Endpoint is redacted to `scheme://host` on read — the full URL plus
   *  the VAPID key is enough to push to that browser, so the server
   *  never returns it. */
  endpoint_origin: string;
  user_agent: string;
  registered_at: number;
}

export interface RevokePushSubscriptionRequest {
  endpoint: string;
}

/* ---------------------------------------------------------------------------
 * Proposals — neuron-mutation review (doc: evolution). Owner reads the list;
 * operators admit / reject.
 * ------------------------------------------------------------------------- */
export interface ProposalNeuron {
  name: string;
  tags: string[];
  version: string | null;
  [k: string]: unknown;
}

export interface ProposalsResponse {
  pending: ProposalNeuron[];
  admitted: ProposalNeuron[];
}

export interface RejectProposalRequest {
  reason: string;
}

/* ---------------------------------------------------------------------------
 * Operator throttles — LLM tiers + resource kinds. All PLATFORM_OPERATOR.
 * ------------------------------------------------------------------------- */
export type LlmTier = "fast" | "reasoning" | "coding" | "image_gen" | "embedding";
export type LlmThrottleMap = Record<LlmTier, number>;

export interface ThrottleResponse {
  throttles: LlmThrottleMap;
}

export interface SetThrottleRequest {
  tier: LlmTier;
  max_daily_calls: number;
}

export type ResourceKind = "sandbox" | "browser" | "egress";

export interface ResourceThrottleRow {
  darwin_id: string;
  kind: ResourceKind;
  max_daily_calls: number;
  installed_at: number;
}

export interface ResourceThrottleResponse {
  throttles: ResourceThrottleRow[];
}

export interface SetResourceThrottleRequest {
  kind: ResourceKind;
  max_daily_calls: number;
}

/* ---------------------------------------------------------------------------
 * Quarantine — operator hold on a single Darwin.
 * ------------------------------------------------------------------------- */
export interface QuarantineRequest {
  reason: string;
  operator?: string;
}

/* ---------------------------------------------------------------------------
 * Wallet spend policy — per-action / per-day / per-lifetime caps.
 * ------------------------------------------------------------------------- */
export interface WalletPolicy {
  per_action_cap_usdc: number | null;
  per_day_cap_usdc: number | null;
  per_lifetime_cap_usdc: number | null;
}

/* ---------------------------------------------------------------------------
 * Fleet-wide operator controls.
 * ------------------------------------------------------------------------- */
export interface FleetPauseResponse {
  paused?: string[];
  resumed?: string[];
}

export interface FleetSpendLockResponse {
  spend_locked?: string[];
  spend_unlocked?: string[];
}

/* ---------------------------------------------------------------------------
 * Patch staging — sandbox eval verdicts (Path-C / amendments).
 * ------------------------------------------------------------------------- */
export interface StagingVerdict {
  promote: boolean;
  reason: string;
  baseline_score: number;
  candidate_score: number;
  sandbox_cost_usdc: number;
  sandbox_session_id: string;
  failure_class: string;
  rejected_path: string | null;
  ran_in_sandbox: boolean;
}

export interface StagingStatusResponse {
  quota: {
    max_concurrent: number;
    active: number;
    total_acquired: number;
    total_rejected: number;
  };
  history_size: number;
  recent: StagingVerdict[];
}

export interface StagingHistoryResponse {
  verdicts: StagingVerdict[];
  total: number;
}

/* ---------------------------------------------------------------------------
 * Signing key — platform attestation key.
 * ------------------------------------------------------------------------- */
export interface SigningPublicKeyResponse {
  format: string;
  public_key: string;
}

/* ---------------------------------------------------------------------------
 * KYC — owner-initiated identity verification.
 * ------------------------------------------------------------------------- */
export interface KycVerdict {
  subject: string;
  status: string;
  tier: string;
  inquiry_id: string;
  reason: string;
  verified_at: number;
  expires_at: number | null;
  is_adult: boolean;
}

export interface KycStartRequest {
  subject?: string | null;
  tier?: "basic" | "standard" | "enhanced";
}

export interface KycStartResponse {
  inquiry_id: string;
  redirect_url: string;
  status: string;
}
