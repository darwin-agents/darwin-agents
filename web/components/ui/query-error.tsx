/**
 * Inline error note for failed list/detail queries. Pages previously
 * rendered their "nothing here yet" empty states on fetch errors too,
 * which made a broken endpoint indistinguishable from an empty Darwin —
 * render this instead when `isError` is set.
 */
export function QueryError({ error, what }: { error: unknown; what: string }) {
  const msg = error instanceof Error ? error.message : String(error ?? "unknown error");
  return (
    <p className="px-4 pb-4 text-sm text-[var(--color-destructive)]">
      Failed to load {what}: {msg}
    </p>
  );
}
