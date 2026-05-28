/**
 * WebSocket client for the multiplexed Darwin stream.
 *
 * Contract (matches src/darwin_platform/api.py `darwin_stream`):
 *   - URL: `${API_BASE_WS}/darwins/{id}/stream`
 *   - Auth: bearer in `Authorization` header (preferred) or `?token=`
 *     query (some hosting setups strip headers on WS upgrades — we fall
 *     back to query automatically when there's a token to pass).
 *   - On open: server sends snapshot.anatomy + snapshot.status, then a
 *     polling stream of timeline.event / status / anatomy.patch /
 *     escalation.open / heartbeat.
 *   - Client may send {type: "subscribe", channels, since}.
 *
 * Behaviour:
 *   - Exponential backoff reconnect (1s → 30s cap)
 *   - Pause stream — buffers messages, applies on resume
 *   - Hands every message to a listener; the React layer turns
 *     timeline.event into TanStack Query cache updates.
 */
import { getAuthToken } from "./auth";

const BASE_URL =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_BASE_URL) ||
  "http://localhost:8000";

const WS_BASE = BASE_URL.replace(/^http/, "ws");

export interface StreamMessage {
  type:
    | "snapshot.anatomy"
    | "snapshot.status"
    | "timeline.event"
    | "anatomy.patch"
    | "status"
    | "escalation.open"
    | "heartbeat"
    | "error";
  payload?: unknown;
  ts?: number;
  code?: string;
  detail?: string;
}

export type ConnectionState = "idle" | "connecting" | "open" | "closed";

export interface StreamOptions {
  channels?: Array<"timeline" | "anatomy" | "status" | "escalations">;
  /** Resync cursor — server sends only events with seq > since. */
  since?: number;
  onMessage: (msg: StreamMessage) => void;
  onStateChange?: (state: ConnectionState) => void;
  /** Initial backoff in ms (default 1000). Capped at 30s. */
  initialBackoffMs?: number;
}

export class DarwinStream {
  private ws: WebSocket | null = null;
  private closedByCaller = false;
  private paused = false;
  private buffer: StreamMessage[] = [];
  private retryMs: number;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private lastSeq: number | undefined;
  private readonly cap = 30_000;

  constructor(
    private readonly darwinId: string,
    private readonly opts: StreamOptions,
  ) {
    this.retryMs = opts.initialBackoffMs ?? 1000;
    this.lastSeq = opts.since;
  }

  start(): void {
    this.closedByCaller = false;
    void this.connect();
  }

  stop(): void {
    this.closedByCaller = true;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    this.ws?.close();
    this.ws = null;
    this.opts.onStateChange?.("closed");
  }

  pause(): void {
    this.paused = true;
  }

  resume(): void {
    if (!this.paused) return;
    this.paused = false;
    const drain = this.buffer;
    this.buffer = [];
    for (const m of drain) this.dispatch(m);
  }

  bufferedCount(): number {
    return this.buffer.length;
  }

  private async connect(): Promise<void> {
    this.opts.onStateChange?.("connecting");
    const token = await getAuthToken();
    const url = new URL(`${WS_BASE}/darwins/${this.darwinId}/stream`);
    // Header auth is preferred (we set it via Sec-WebSocket-Protocol? No —
    // browsers don't expose custom headers on WS. Use query-param fallback.)
    if (token) url.searchParams.set("token", token);

    let ws: WebSocket;
    try {
      ws = new WebSocket(url.toString());
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      this.opts.onStateChange?.("open");
      // Reset backoff after a stable open.
      this.retryMs = this.opts.initialBackoffMs ?? 1000;
      // Resync: tell the server what we've already seen.
      const subscribe: Record<string, unknown> = {
        type: "subscribe",
        channels: this.opts.channels ?? ["timeline", "anatomy", "status", "escalations"],
      };
      if (this.lastSeq !== undefined) subscribe.since = this.lastSeq;
      ws.send(JSON.stringify(subscribe));
    };

    ws.onmessage = (evt) => {
      let parsed: StreamMessage;
      try {
        parsed = JSON.parse(typeof evt.data === "string" ? evt.data : "") as StreamMessage;
      } catch {
        return;
      }
      // Track the latest seq so a reconnect can resync.
      if (parsed.type === "timeline.event" && parsed.payload && typeof parsed.payload === "object") {
        const seq = (parsed.payload as { seq?: number }).seq;
        if (typeof seq === "number") this.lastSeq = seq;
      }
      if (this.paused) {
        // Heartbeat keeps flowing so the UI can show "buffered N".
        if (parsed.type !== "heartbeat") {
          this.buffer.push(parsed);
        }
        return;
      }
      this.dispatch(parsed);
    };

    ws.onerror = () => {
      // Will be followed by onclose; reconnect is handled there.
    };

    ws.onclose = () => {
      this.ws = null;
      if (this.closedByCaller) return;
      this.scheduleReconnect();
    };
  }

  private dispatch(msg: StreamMessage): void {
    try {
      this.opts.onMessage(msg);
    } catch {
      // A buggy listener must not break the stream.
    }
  }

  private scheduleReconnect(): void {
    this.opts.onStateChange?.("idle");
    const delay = this.retryMs;
    this.retryMs = Math.min(this.cap, this.retryMs * 2);
    this.timer = setTimeout(() => {
      void this.connect();
    }, delay);
  }
}
