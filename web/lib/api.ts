/**
 * Typed fetch wrapper around the Darwin Platform API.
 *
 * Contract (docs/frontend/03-architecture-and-api-integration.md):
 *   - Base URL from NEXT_PUBLIC_API_BASE_URL
 *   - Authorization: Bearer <token from lib/auth>
 *   - Idempotency-Key on every mutating POST (lifecycle + money)
 *   - 4xx/5xx surface as ApiError{status, code, detail}
 *   - Never auto-retry POST /kill (caller decides)
 */
import { getAuthToken } from "./auth";
import type {
  AlertsResponse,
  BillingResponse,
  DarwinEvent,
  DarwinFiatPayoutRequest,
  DarwinFiatRefundRequest,
  DarwinListResponse,
  DarwinStatus,
  EscalationResponseRequest,
  EscalationsResponse,
  EventsResponse,
  FiatConfirmDepositRequest,
  FiatDepositQuoteRequest,
  FiatWithdrawRequest,
  ForensicManifest,
  ForensicSnapshotListResponse,
  FreelancerDestination,
  HarvestRequest,
  HarvestRuleRequest,
  LedgerSummary,
  OrganismRowsResponse,
  OrganismTablesResponse,
  PlatformCapabilities,
  PushSubscriptionRequest,
  PushSubscriptionRow,
  RevokePushSubscriptionRequest,
  VapidPublicKeyResponse,
  LedgerResponse,
  LineageTreeResponse,
  OmnibusEntryDTO,
  OwnerControls,
  PreApprovalRequest,
  PreApprovalRule,
  RegisterFreelancerRequest,
  RetryOmnibusFundRequest,
  SpawnRequest,
  VaultPutRequest,
} from "./types";

const BASE_URL =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_BASE_URL) ||
  "http://localhost:8000";

const CLIENT_HEADER = "X-Darwin-Client";
const CLIENT_VERSION = "web/0.1.0";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: unknown;

  constructor(status: number, code: string, detail: unknown, message?: string) {
    super(message ?? `HTTP ${status}: ${code}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }

  get isAuth(): boolean {
    return this.status === 401;
  }

  get isForbidden(): boolean {
    return this.status === 403;
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }

  get isConflict(): boolean {
    return this.status === 409;
  }
}

interface RequestOpts {
  method?: "GET" | "POST" | "DELETE" | "PUT" | "PATCH";
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
  idempotencyKey?: string;
  signal?: AbortSignal;
  // Mutation actions (kill, harvest, respond) — Idempotency-Key is auto-set.
  mutating?: boolean;
  // Override the bearer token (used by Playwright + dev token paste UX).
  token?: string | null;
}

function buildUrl(path: string, query?: RequestOpts["query"]): string {
  const url = new URL(path.startsWith("http") ? path : `${BASE_URL}${path}`);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null) continue;
      url.searchParams.set(k, String(v));
    }
  }
  return url.toString();
}

function generateIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `idem-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export async function apiFetch<T>(path: string, opts: RequestOpts = {}): Promise<T> {
  const method = opts.method ?? "GET";
  const token = opts.token !== undefined ? opts.token : await getAuthToken();
  const headers: Record<string, string> = {
    Accept: "application/json",
    [CLIENT_HEADER]: CLIENT_VERSION,
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let body: BodyInit | undefined;
  if (opts.body !== undefined && method !== "GET" && method !== "DELETE") {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }

  if (opts.mutating || opts.idempotencyKey) {
    headers["Idempotency-Key"] = opts.idempotencyKey ?? generateIdempotencyKey();
  }

  const res = await fetch(buildUrl(path, opts.query), {
    method,
    headers,
    body,
    signal: opts.signal,
    cache: "no-store",
  });

  if (!res.ok) {
    let code = `http_${res.status}`;
    let detail: unknown = null;
    try {
      const text = await res.text();
      if (text) {
        try {
          const parsed = JSON.parse(text);
          detail = parsed;
          if (typeof parsed === "object" && parsed !== null) {
            const maybe = parsed as Record<string, unknown>;
            if (typeof maybe.detail === "string") code = maybe.detail;
            else if (typeof maybe.code === "string") code = maybe.code;
          }
        } catch {
          detail = text;
        }
      }
    } catch {
      /* swallow */
    }
    throw new ApiError(res.status, code, detail);
  }

  if (res.status === 204) return undefined as T;
  // Handle text/csv etc. — caller can re-fetch with a raw helper if needed.
  const ct = res.headers.get("Content-Type") ?? "";
  if (ct.includes("application/json")) return (await res.json()) as T;
  return (await res.text()) as unknown as T;
}

/* ----------------------------------------------------------------------------
 * Typed endpoints (subset the UI consumes — full coverage arrives via codegen)
 * ------------------------------------------------------------------------- */

export const api = {
  // Lifecycle
  listDarwins: () => apiFetch<DarwinListResponse>("/darwins"),
  getDarwin: (id: string) => apiFetch<DarwinStatus>(`/darwins/${id}`),
  spawn: (req: SpawnRequest) =>
    apiFetch<DarwinStatus>("/darwins", { method: "POST", body: req, mutating: true }),
  pause: (id: string, reason?: string) =>
    apiFetch<{ status: string }>(`/darwins/${id}/pause`, {
      method: "POST",
      body: { reason: reason ?? "owner paused" },
      mutating: true,
    }),
  resume: (id: string) =>
    apiFetch<{ status: string }>(`/darwins/${id}/resume`, { method: "POST", mutating: true }),
  kill: (id: string, reason: string, idempotencyKey: string) =>
    apiFetch<{ status: string }>(`/darwins/${id}/kill`, {
      method: "POST",
      body: { reason },
      idempotencyKey,
    }),
  forceReproduce: (id: string) =>
    apiFetch<{ status: string }>(`/darwins/${id}/reproduce/force`, {
      method: "POST",
      mutating: true,
    }),

  // Events
  events: (
    id: string,
    params: {
      limit?: number;
      offset?: number;
      since?: number;
      cursor?: number;
      kind?: string;
      neuron_id?: string;
      tier?: string;
    } = {},
  ) =>
    apiFetch<EventsResponse & { next_cursor?: number | null }>(
      `/darwins/${id}/events`,
      {
        query: {
          limit: params.limit ?? 100,
          offset: params.since === undefined && params.cursor === undefined ? (params.offset ?? 0) : undefined,
          since: params.since,
          cursor: params.cursor,
          kind: params.kind,
          neuron_id: params.neuron_id,
          tier: params.tier,
        },
      },
    ),

  // Anatomy (M3-A)
  anatomy: (id: string) =>
    apiFetch<{
      darwin_id: string;
      orchestrator: { id: string; label: string; state: string; tick: number | null };
      neurons: Array<{
        id: string;
        label: string;
        category: string;
        version: string | null;
        status: "idle" | "active" | "disabled" | "error";
        last_dispatch_at: number | null;
        last_error_at: number | null;
        dispatch_count_recent: number;
      }>;
      edges: Array<{
        from: string;
        to: string;
        kind: string;
        active: boolean;
        weight: number;
      }>;
      heatmap: Record<string, number>;
      dna: { generation: number | null; layers: string[] };
    }>(`/darwins/${id}/anatomy`),

  // Aggregate fleet inbox (M2-B optimization)
  fleetEscalations: (includeResolved = false) =>
    apiFetch<{
      include_resolved: boolean;
      tickets: Array<Record<string, unknown>>;
    }>("/escalations", { query: { include_resolved: includeResolved } }),

  // Ledger / billing
  ledger: (id: string, params: { limit?: number; offset?: number } = {}) =>
    apiFetch<LedgerResponse>(`/darwins/${id}/ledger`, {
      query: { limit: params.limit ?? 100, offset: params.offset ?? 0 },
    }),
  ledgerCsvUrl: (id: string) => buildUrl(`/darwins/${id}/ledger.csv`),
  billing: (id: string) => apiFetch<BillingResponse>(`/darwins/${id}/billing`),
  // Jurisdiction-neutral accounting summary (docs 05, 17). `year` (UTC) scopes
  // to a tax year; omit for lifetime. Owner-scoped — not a statutory form.
  ledgerSummary: (id: string, year?: number) =>
    apiFetch<LedgerSummary>(`/darwins/${id}/ledger/summary`, {
      query: { year },
    }),

  // Controls
  controls: (id: string) => apiFetch<OwnerControls>(`/darwins/${id}/controls`),
  setSpendLock: (id: string, locked: boolean, reason = "owner spend-lock") =>
    apiFetch<OwnerControls>(`/darwins/${id}/controls/spend_lock`, {
      method: "POST",
      body: { locked, reason },
      mutating: true,
    }),
  setReproductionBlock: (id: string, blocked: boolean) =>
    apiFetch<OwnerControls>(`/darwins/${id}/controls/reproduction_block`, {
      method: "POST",
      body: { blocked },
      mutating: true,
    }),
  setDisabledNeurons: (id: string, neurons: string[]) =>
    apiFetch<OwnerControls>(`/darwins/${id}/controls/disabled_neurons`, {
      method: "POST",
      body: { neurons },
      mutating: true,
    }),
  setDisabledCategories: (id: string, categories: string[]) =>
    apiFetch<OwnerControls>(`/darwins/${id}/controls/disabled_categories`, {
      method: "POST",
      body: { categories },
      mutating: true,
    }),

  // Escalations
  escalations: (id: string, includeResolved = false) =>
    apiFetch<EscalationsResponse>(`/darwins/${id}/escalations`, {
      query: { include_resolved: includeResolved },
    }),
  respondEscalation: (
    id: string,
    escalationId: string,
    decision: "approve" | "deny" | "data",
    data?: Record<string, unknown>,
  ) => {
    const body: EscalationResponseRequest = { decision, data: data ?? null };
    return apiFetch<{ status: string }>(
      `/darwins/${id}/escalations/${escalationId}/respond`,
      { method: "POST", body, mutating: true },
    );
  },

  // Lineage
  lineageTree: (id: string, maxDepth = 16) =>
    apiFetch<LineageTreeResponse>(`/darwins/${id}/lineage/tree`, {
      query: { max_depth: maxDepth },
    }),

  // Alerts
  alerts: (id: string) => apiFetch<AlertsResponse>(`/darwins/${id}/alerts`),

  // Harvest. `idempotencyKey` lets the caller pin a stable key for the
  // lifetime of a confirm dialog so re-pressing never moves funds twice;
  // omit it and a fresh key is generated per call (`mutating`).
  harvest: (id: string, req: HarvestRequest, idempotencyKey?: string) =>
    apiFetch<{ status: string }>(`/darwins/${id}/harvest`, {
      method: "POST",
      body: req,
      idempotencyKey,
      mutating: true,
    }),

  listHarvestRules: (id: string) =>
    apiFetch<{ darwin_id: string; rules: Array<Record<string, unknown>> }>(
      `/darwins/${id}/harvest_rules`,
    ),
  addHarvestRule: (id: string, req: HarvestRuleRequest) =>
    apiFetch<Record<string, unknown>>(`/darwins/${id}/harvest_rules`, {
      method: "POST",
      body: req,
      mutating: true,
    }),
  deleteHarvestRule: (id: string, ruleId: string) =>
    apiFetch<{ status: string }>(`/darwins/${id}/harvest_rules/${ruleId}`, {
      method: "DELETE",
      mutating: true,
    }),

  // Vault / resources
  listVault: (id: string) =>
    apiFetch<{ darwin_id: string; entries: Array<Record<string, unknown>> }>(
      `/darwins/${id}/vault`,
    ),
  putVault: (id: string, req: VaultPutRequest) =>
    apiFetch<Record<string, unknown>>(`/darwins/${id}/vault`, {
      method: "POST",
      body: req,
      mutating: true,
    }),
  deleteVault: (id: string, tier: string, key: string) =>
    apiFetch<{ status: string }>(`/darwins/${id}/vault`, {
      method: "DELETE",
      query: { tier, key },
      mutating: true,
    }),
  listResources: (id: string) =>
    apiFetch<{ darwin_id: string; resources: Array<Record<string, unknown>> }>(
      `/darwins/${id}/resources`,
    ),
  deleteResource: (id: string, resourceId: string) =>
    apiFetch<{ status: string }>(`/darwins/${id}/resources/${resourceId}`, {
      method: "DELETE",
      mutating: true,
    }),

  // Browser sessions (M2-B captcha takeover)
  browserSessions: (id: string) =>
    apiFetch<{ darwin_id: string; sessions: Array<Record<string, unknown>> }>(
      `/darwins/${id}/browser/sessions`,
    ),

  // Fiat (M2-D)
  fiatDepositQuote: (req: FiatDepositQuoteRequest) =>
    apiFetch<Record<string, unknown>>(`/platform/fiat/deposit_quote`, {
      method: "POST",
      body: req,
      mutating: true,
    }),
  fiatConfirmDeposit: (req: FiatConfirmDepositRequest) =>
    apiFetch<Record<string, unknown>>(`/platform/fiat/confirm_deposit`, {
      method: "POST",
      body: req,
      mutating: true,
    }),
  fiatWithdraw: (req: FiatWithdrawRequest) =>
    apiFetch<Record<string, unknown>>(`/platform/fiat/withdraw`, {
      method: "POST",
      body: req,
      mutating: true,
    }),

  /* -------------------------------------------------------------------------
   * Payments — doc 17 (preapprovals, freelancers, fiat payouts, omnibus retry)
   * --------------------------------------------------------------------- */

  // Pre-approvals
  listPreapprovals: (id: string) =>
    apiFetch<PreApprovalRule[]>(`/platform/wallets/${id}/preapprovals`),
  setPreapproval: (id: string, ruleId: string, req: PreApprovalRequest) =>
    apiFetch<PreApprovalRule>(
      `/platform/wallets/${id}/preapprovals/${ruleId}`,
      { method: "PUT", body: req, mutating: true },
    ),
  revokePreapproval: (id: string, ruleId: string) =>
    apiFetch<{ revoked: boolean; rule_id: string }>(
      `/platform/wallets/${id}/preapprovals/${ruleId}`,
      { method: "DELETE", mutating: true },
    ),

  // Freelancer registry — operator scoped routes.
  listFreelancers: (ownerSubject: string) =>
    apiFetch<FreelancerDestination[]>(`/platform/freelancers/${ownerSubject}`),
  registerFreelancer: (req: RegisterFreelancerRequest) =>
    apiFetch<FreelancerDestination>("/platform/freelancers", {
      method: "POST",
      body: req,
      mutating: true,
    }),
  revokeFreelancer: (ownerSubject: string, freelancerId: string) =>
    apiFetch<{ revoked: boolean; freelancer_id: string }>(
      `/platform/freelancers/${ownerSubject}/${freelancerId}`,
      { method: "DELETE", mutating: true },
    ),

  // Freelancer registry — owner self-serve (uses ctx.subject implicitly).
  listMyFreelancers: () =>
    apiFetch<FreelancerDestination[]>("/me/freelancers"),
  registerMyFreelancer: (req: RegisterFreelancerRequest) =>
    apiFetch<FreelancerDestination>("/me/freelancers", {
      method: "POST",
      body: req,
      mutating: true,
    }),
  revokeMyFreelancer: (freelancerId: string) =>
    apiFetch<{ revoked: boolean; freelancer_id: string }>(
      `/me/freelancers/${freelancerId}`,
      { method: "DELETE", mutating: true },
    ),

  // Darwin → fiat payout (and refund / omnibus fund retry)
  darwinFiatPayment: (id: string, req: DarwinFiatPayoutRequest) =>
    apiFetch<OmnibusEntryDTO>(`/darwins/${id}/fiat_payment`, {
      method: "POST",
      body: req,
      mutating: true,
    }),
  darwinFiatRefund: (id: string, req: DarwinFiatRefundRequest) =>
    apiFetch<OmnibusEntryDTO>(`/darwins/${id}/fiat_payment/refund`, {
      method: "POST",
      body: req,
      mutating: true,
    }),
  retryOmnibusFund: (id: string, req: RetryOmnibusFundRequest) =>
    apiFetch<OmnibusEntryDTO>(`/darwins/${id}/omnibus/fund`, {
      method: "POST",
      body: req,
      mutating: true,
    }),

  /* -------------------------------------------------------------------------
   * Operator data browser & post-mortem forensics (docs 05, 08, 22)
   * --------------------------------------------------------------------- */

  // Deployment capabilities — the dashboard gates organism/forensics tabs on
  // this so they're hidden when the deployment (or token) can't serve them.
  capabilities: () =>
    apiFetch<PlatformCapabilities>("/platform/capabilities"),

  // Organism (business) DB — read through the SELECT-only dashboard role.
  organismTables: (id: string) =>
    apiFetch<OrganismTablesResponse>(
      `/platform/darwins/${id}/organism/tables`,
    ),
  organismRows: (
    id: string,
    table: string,
    params: { limit?: number; offset?: number } = {},
  ) =>
    apiFetch<OrganismRowsResponse>(
      `/platform/darwins/${id}/organism/tables/${encodeURIComponent(table)}/rows`,
      { query: { limit: params.limit ?? 50, offset: params.offset ?? 0 } },
    ),

  // Death-time soma forensics — index (metadata) + per-snapshot manifest.
  forensics: (id: string) =>
    apiFetch<ForensicSnapshotListResponse>(
      `/platform/darwins/${id}/forensics`,
    ),
  forensicSnapshot: (id: string, snapshotId: string) =>
    apiFetch<ForensicManifest>(
      `/platform/darwins/${id}/forensics/${encodeURIComponent(snapshotId)}`,
    ),

  // Health — readyz is the dashboard's liveness probe (see ApiBanner).
  readyz: () => apiFetch<unknown>("/readyz"),

  /* -------------------------------------------------------------------------
   * Push subscriptions (doc 22 — PWA escalation companion)
   * --------------------------------------------------------------------- */

  getPushVapidPublicKey: () =>
    apiFetch<VapidPublicKeyResponse>("/me/push-vapid-public-key"),
  registerPushSubscription: (req: PushSubscriptionRequest) =>
    apiFetch<{
      owner_subject: string;
      endpoint_origin: string;
      registered: boolean;
    }>("/me/push-subscriptions", { method: "POST", body: req, mutating: true }),
  listPushSubscriptions: () =>
    apiFetch<PushSubscriptionRow[]>("/me/push-subscriptions"),
  revokePushSubscription: (req: RevokePushSubscriptionRequest) =>
    apiFetch<{ revoked: boolean }>("/me/push-subscriptions", {
      method: "DELETE",
      query: { endpoint: req.endpoint },
      mutating: true,
    }),
};

export type Api = typeof api;

// Re-exported helpers for components that need URLs (e.g. ledger CSV anchor).
export { buildUrl as buildApiUrl };

// Convenience helper used by the events table to merge polled pages.
// `seq` is the canonical identifier from the runtime; `hash` and the
// pre-codegen `event_id` are accepted as fallbacks so older payloads
// (or third-party event injectors) still dedupe correctly.
export function dedupeEvents(rows: DarwinEvent[]): DarwinEvent[] {
  const seen = new Set<string | number>();
  const out: DarwinEvent[] = [];
  for (const ev of rows) {
    if (!ev) continue;
    const key = ev.seq ?? ev.hash ?? ev.event_id;
    if (key === undefined || seen.has(key)) continue;
    seen.add(key);
    out.push(ev);
  }
  return out;
}
