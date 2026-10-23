import type { ReactNode } from "react";

/** Public legal page shell. The text is a DRAFT until counsel replaces it —
 *  launch is gated on that review (docs/engineering/32 §C4). */
export function LegalPage({ title, children }: { title: string; children: ReactNode }) {
  return (
    <main className="mx-auto max-w-2xl space-y-4 px-4 py-10 text-sm leading-6">
      <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
      <p className="rounded-md border border-[var(--color-alert-warning)]/40 bg-[var(--color-alert-warning)]/10 px-3 py-2 text-xs">
        Draft pending legal review. It states how the product works; it is not yet the binding
        agreement.
      </p>
      {children}
    </main>
  );
}
