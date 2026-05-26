"use client";

/**
 * Dev-only token gate. Shows a paste-token form when no bearer is configured.
 * Production swaps this for Clerk's <ClerkProvider> + <SignedIn>/<SignedOut>.
 */

import { useEffect, useState, type ReactNode } from "react";
import { clearDevToken, getDevToken, setDevToken } from "@/lib/auth";

export function DevAuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setToken(getDevToken());
    setReady(true);
  }, []);

  if (!ready) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-[var(--color-muted-foreground)]">
        Loading…
      </div>
    );
  }

  if (!token) {
    return <TokenForm onSubmit={(t) => { setDevToken(t); setToken(t); }} />;
  }

  return (
    <>
      {children}
      <DevTokenChip
        token={token}
        onClear={() => {
          clearDevToken();
          setToken(null);
        }}
      />
    </>
  );
}

function TokenForm({ onSubmit }: { onSubmit: (token: string) => void }) {
  const [value, setValue] = useState("");
  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form
        className="w-full max-w-md space-y-4 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-6"
        onSubmit={(e) => {
          e.preventDefault();
          if (value.trim()) onSubmit(value.trim());
        }}
      >
        <div>
          <h1 className="text-lg font-semibold">Darwin dev sign-in</h1>
          <p className="mt-1 text-sm text-[var(--color-muted-foreground)]">
            Paste a bearer token from the platform&apos;s in-memory{" "}
            <code>TokenStore</code>. Production replaces this with Clerk.
          </p>
        </div>
        <input
          type="password"
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="darwin_dev_token"
          className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-background)] px-3 py-2 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]"
        />
        <button
          type="submit"
          disabled={!value.trim()}
          className="w-full rounded-md bg-[var(--color-accent)] px-3 py-2 text-sm font-medium text-[var(--color-accent-foreground)] disabled:opacity-50"
        >
          Continue
        </button>
      </form>
    </div>
  );
}

function DevTokenChip({ token, onClear }: { token: string; onClear: () => void }) {
  const short = token.length > 12 ? `${token.slice(0, 6)}…${token.slice(-4)}` : token;
  return (
    <div className="pointer-events-auto fixed bottom-3 right-3 z-50 flex items-center gap-2 rounded-full border border-[var(--color-border)] bg-[var(--color-card)] px-3 py-1 text-xs text-[var(--color-muted-foreground)] shadow">
      <span className="mono">{short}</span>
      <button
        type="button"
        onClick={onClear}
        className="text-[var(--color-destructive)] hover:underline"
      >
        sign out
      </button>
    </div>
  );
}
