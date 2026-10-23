"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { formatRelative, formatTimestamp } from "@/lib/format";
import type { EscalationTicket } from "@/lib/types";

type Decision = "approve" | "deny" | "data";

/**
 * Escalation respond dialog. No optimistic close — legal/safety: the form
 * stays disabled until the server confirms (docs/frontend/07).
 *
 * Per-class UX is dispatched from `ticket.ticket_class`:
 *   - approval / kyc / captcha / info → approve / deny + note
 *   - data                              → structured JSON entry
 *   - unknown                           → both modes available
 */
export function RespondDialog({
  ticket,
  open,
  onOpenChange,
}: {
  ticket: EscalationTicket | null;
  open: boolean;
  onOpenChange: (next: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [decision, setDecision] = useState<Decision>("approve");
  const [note, setNote] = useState("");
  const [dataJson, setDataJson] = useState("{}");

  const respond = useMutation({
    mutationFn: () => {
      if (!ticket) throw new Error("no ticket");
      const payload: Record<string, unknown> | undefined =
        decision === "data" ? safeParse(dataJson) : note.trim() ? { note: note.trim() } : undefined;
      return api.respondEscalation(ticket.darwin_id, ticket.ticket_id, decision, payload);
    },
    onSuccess: () => {
      if (!ticket) return;
      queryClient.invalidateQueries({ queryKey: queryKeys.escalations(ticket.darwin_id) });
      queryClient.invalidateQueries({ queryKey: queryKeys.fleetEscalations() });
      queryClient.invalidateQueries({ queryKey: queryKeys.events(ticket.darwin_id, 0) });
      onOpenChange(false);
      // Reset for next open.
      setNote("");
      setDataJson("{}");
      setDecision("approve");
    },
  });

  if (!ticket) return null;

  const klass = ticket.ticket_class ?? "ticket";
  const remaining = ticket.deadline_ts ? formatRelative(ticket.deadline_ts) : null;
  const captchaUrl = pickEvidenceUrl(ticket);

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (respond.isPending) return;
        onOpenChange(o);
      }}
    >
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>
            Respond · <span className="capitalize">{klass}</span>
          </DialogTitle>
          <DialogDescription>
            {ticket.neuron && (
              <>
                Raised by <span className="mono">{ticket.neuron}</span>
                {remaining && <> · due {remaining}</>}
              </>
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-sm">
          <DetailSummary ticket={ticket} />

          {klass === "captcha" && captchaUrl && (
            <a
              href={captchaUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-muted)] px-3 py-2 text-xs hover:bg-[var(--color-card)]"
            >
              <ExternalLink className="size-3" />
              Open browser session (Browserbase / Browser Use Cloud)
            </a>
          )}
          {klass === "kyc" && captchaUrl && (
            <a
              href={captchaUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-muted)] px-3 py-2 text-xs hover:bg-[var(--color-card)]"
            >
              <ExternalLink className="size-3" />
              Open KYC flow (Persona / Onfido)
            </a>
          )}

          <div>
            <label className="mb-1 block text-xs tracking-wide text-[var(--color-muted-foreground)] uppercase">
              Decision
            </label>
            <div className="flex gap-2">
              <DecisionButton
                value="approve"
                current={decision}
                onClick={setDecision}
                disabled={respond.isPending}
              />
              <DecisionButton
                value="deny"
                current={decision}
                onClick={setDecision}
                disabled={respond.isPending}
              />
              <DecisionButton
                value="data"
                current={decision}
                onClick={setDecision}
                disabled={respond.isPending}
              />
            </div>
          </div>

          {decision === "data" ? (
            <div>
              <label className="mb-1 block text-xs tracking-wide text-[var(--color-muted-foreground)] uppercase">
                Structured data (JSON)
              </label>
              <textarea
                className="mono w-full rounded-md border border-[var(--color-border)] bg-[var(--color-background)] p-2 text-xs"
                rows={6}
                value={dataJson}
                onChange={(e) => setDataJson(e.target.value)}
                disabled={respond.isPending}
              />
              {!safeParse(dataJson) && (
                <p className="mt-1 text-xs text-[var(--color-destructive)]">Not valid JSON.</p>
              )}
            </div>
          ) : (
            <div>
              <label className="mb-1 block text-xs tracking-wide text-[var(--color-muted-foreground)] uppercase">
                Note (optional)
              </label>
              <Input
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="Audited with the response"
                disabled={respond.isPending}
              />
            </div>
          )}

          {respond.isError && <ErrorPanel error={respond.error} />}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={respond.isPending}
          >
            Cancel
          </Button>
          <Button
            disabled={respond.isPending || (decision === "data" && !safeParse(dataJson))}
            onClick={() => respond.mutate()}
            variant={decision === "deny" ? "destructive" : "default"}
          >
            {respond.isPending ? "Sending…" : `Confirm ${decision}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DecisionButton({
  value,
  current,
  onClick,
  disabled,
}: {
  value: Decision;
  current: Decision;
  onClick: (v: Decision) => void;
  disabled?: boolean;
}) {
  const active = current === value;
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => onClick(value)}
      className={
        active
          ? "rounded-md bg-[var(--color-accent)] px-3 py-1.5 text-xs font-medium text-[var(--color-accent-foreground)] capitalize"
          : "rounded-md border border-[var(--color-border)] px-3 py-1.5 text-xs text-[var(--color-muted-foreground)] capitalize hover:bg-[var(--color-muted)]"
      }
    >
      {value}
    </button>
  );
}

function DetailSummary({ ticket }: { ticket: EscalationTicket }) {
  const opened = ticket.opened_ts ? formatTimestamp(ticket.opened_ts) : null;
  const detailKeys = Object.entries(ticket.detail ?? {}).filter(([, v]) => v !== null && v !== "");
  return (
    <div className="space-y-1 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-3 text-xs">
      <div className="mono text-[var(--color-muted-foreground)]">{ticket.ticket_id}</div>
      {opened && <div className="text-[var(--color-muted-foreground)]">opened {opened}</div>}
      {detailKeys.length > 0 && (
        <dl className="mt-2 grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1">
          {detailKeys.map(([k, v]) => (
            <DetailRow key={k} k={k} v={v} />
          ))}
        </dl>
      )}
    </div>
  );
}

function DetailRow({ k, v }: { k: string; v: unknown }) {
  const text = typeof v === "string" ? v : JSON.stringify(v);
  return (
    <>
      <dt className="text-[var(--color-muted-foreground)]">{k}</dt>
      <dd className="mono truncate" title={text}>
        {text}
      </dd>
    </>
  );
}

function pickEvidenceUrl(ticket: EscalationTicket): string | null {
  const candidates = ["live_view_url", "session_url", "handoff_url", "evidence_url", "url"];
  const detail = ticket.detail ?? {};
  for (const k of candidates) {
    const v = detail[k];
    if (typeof v === "string" && /^https?:\/\//.test(v)) return v;
  }
  return null;
}

function safeParse(s: string): Record<string, unknown> | undefined {
  try {
    const v = JSON.parse(s);
    if (v && typeof v === "object" && !Array.isArray(v)) return v as Record<string, unknown>;
    return undefined;
  } catch {
    return undefined;
  }
}

function ErrorPanel({ error }: { error: unknown }) {
  let msg = "Unknown error";
  if (error instanceof ApiError) {
    msg =
      error.status === 400
        ? "Decision rejected by the platform — check the ticket state."
        : `${error.status} · ${error.code}`;
  } else if (error instanceof Error) {
    msg = error.message;
  }
  return (
    <div className="rounded border border-[var(--color-destructive)]/40 bg-[var(--color-destructive)]/10 px-3 py-2 text-xs text-[var(--color-destructive)]">
      {msg}
    </div>
  );
}
