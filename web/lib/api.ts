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
  DarwinListResponse,
  DarwinStatus,
  EscalationsResponse,
  EventsResponse,
  LedgerResponse,
  LineageTreeResponse,
  OwnerControls,
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
  spawn: (req: { opening_usdc: number; escalation_auto?: string | null; parent_id?: string | null }) =>
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
  events: (id: string, params: { limit?: number; offset?: number } = {}) =>
    apiFetch<EventsResponse>(`/darwins/${id}/events`, {
      query: { limit: params.limit ?? 100, offset: params.offset ?? 0 },
    }),

  // Ledger / billing
  ledger: (id: string, params: { limit?: number; offset?: number } = {}) =>
    apiFetch<LedgerResponse>(`/darwins/${id}/ledger`, {
      query: { limit: params.limit ?? 100, offset: params.offset ?? 0 },
    }),
  ledgerCsvUrl: (id: string) => buildUrl(`/darwins/${id}/ledger.csv`),
  billing: (id: string) => apiFetch<BillingResponse>(`/darwins/${id}/billing`),

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
  ) =>
    apiFetch<{ status: string }>(`/darwins/${id}/escalations/${escalationId}/respond`, {
      method: "POST",
      body: { decision, data: data ?? null },
      mutating: true,
    }),

  // Lineage
  lineageTree: (id: string, maxDepth = 16) =>
    apiFetch<LineageTreeResponse>(`/darwins/${id}/lineage/tree`, {
      query: { max_depth: maxDepth },
    }),

  // Alerts
  alerts: (id: string) => apiFetch<AlertsResponse>(`/darwins/${id}/alerts`),

  // Harvest
  harvest: (id: string, amount_usdc: number, destination: string) =>
    apiFetch<{ status: string }>(`/darwins/${id}/harvest`, {
      method: "POST",
      body: { amount_usdc, destination },
      mutating: true,
    }),

  // Health
  healthz: () => apiFetch<unknown>("/healthz"),
  readyz: () => apiFetch<unknown>("/readyz"),
};

export type Api = typeof api;

// Re-exported helpers for components that need URLs (e.g. ledger CSV anchor).
export { buildUrl as buildApiUrl };

// Convenience helper used by the events table to merge polled pages.
export function dedupeEvents(rows: DarwinEvent[]): DarwinEvent[] {
  const seen = new Set<string>();
  const out: DarwinEvent[] = [];
  for (const ev of rows) {
    if (!ev || seen.has(ev.event_id)) continue;
    seen.add(ev.event_id);
    out.push(ev);
  }
  return out;
}
