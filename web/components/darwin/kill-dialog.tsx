"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Skull } from "lucide-react";
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
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

/**
 * Type-the-id confirmation per docs/frontend/10 §M1-D. The Idempotency-Key is
 * generated *once per modal open* — re-pressing the button reuses it so a flaky
 * network never produces two kill orders. NEVER retried automatically.
 */
export function KillDialog({
  darwinId,
  trigger,
}: {
  darwinId: string;
  trigger?: React.ReactNode;
}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [reason, setReason] = useState("owner kill");
  const [idempotencyKey] = useState(() => crypto.randomUUID());

  const mutation = useMutation({
    mutationFn: () => api.kill(darwinId, reason, idempotencyKey),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.darwins() });
      queryClient.invalidateQueries({ queryKey: queryKeys.darwin(darwinId) });
      setOpen(false);
      router.push("/");
    },
  });

  const matches = confirmText.trim() === darwinId;

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        // Refuse to close while a kill is in flight — no double-submit.
        if (mutation.isPending) return;
        setOpen(o);
        if (!o) setConfirmText("");
      }}
    >
      <DialogTrigger asChild>
        {trigger ?? (
          <Button variant="destructive" size="sm">
            <Skull className="mr-1 size-4" />
            Kill
          </Button>
        )}
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Kill Darwin</DialogTitle>
          <DialogDescription>
            This is <strong>permanent</strong>. The Darwin&apos;s wallet is locked, all in-flight
            commitments are abandoned, and no resurrection is possible. Treasury that has not
            already been harvested is forfeited to the death sequence.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-sm">
          <div>
            <label htmlFor="kill-id" className="mb-1 block text-xs uppercase tracking-wide text-[var(--color-muted-foreground)]">
              Type the Darwin id to confirm
            </label>
            <Input
              id="kill-id"
              autoFocus
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              placeholder={darwinId}
              className="font-mono"
              autoComplete="off"
              disabled={mutation.isPending}
            />
            <div className="mono mt-1 text-[11px] text-[var(--color-muted-foreground)]">
              {darwinId}
            </div>
          </div>
          <div>
            <label htmlFor="kill-reason" className="mb-1 block text-xs uppercase tracking-wide text-[var(--color-muted-foreground)]">
              Reason (audited)
            </label>
            <Input
              id="kill-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              disabled={mutation.isPending}
            />
          </div>

          {mutation.isError && <KillErrorPanel error={mutation.error} darwinId={darwinId} />}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => setOpen(false)}
            disabled={mutation.isPending}
          >
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={!matches || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? "Killing…" : "Confirm kill"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function KillErrorPanel({ error, darwinId }: { error: unknown; darwinId: string }) {
  if (error instanceof ApiError && error.isConflict) {
    return (
      <div className="rounded border border-[var(--color-alert-throttle)]/40 bg-[var(--color-alert-throttle)]/10 px-3 py-2 text-xs text-[var(--color-alert-throttle)]">
        Darwin {darwinId} is already terminal. Refresh to see latest state.
      </div>
    );
  }
  const msg = error instanceof Error ? error.message : "Unknown error";
  return (
    <div className="rounded border border-[var(--color-destructive)]/40 bg-[var(--color-destructive)]/10 px-3 py-2 text-xs text-[var(--color-destructive)]">
      Kill failed: {msg}. Do <strong>not</strong> retry blindly — verify state first.
    </div>
  );
}
