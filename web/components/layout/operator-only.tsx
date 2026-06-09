import { ShieldAlert } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";

/**
 * Shown in place of an operator-only console when the caller is an owner
 * (no `PLATFORM_OPERATOR` scope). The matching tab is hidden in the
 * per-Darwin nav (`darwins/[id]/page.tsx` gates on `caps.is_operator`);
 * this guards the page body for direct-URL access so an owner sees a clear
 * notice instead of a console whose every action would 403.
 */
export function OperatorOnly({ title }: { title: string }) {
  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-4">
      <Card>
        <CardContent className="flex flex-col items-center gap-3 py-12 text-center">
          <ShieldAlert className="size-8 text-[var(--color-muted-foreground)]" />
          <div>
            <h1 className="text-lg font-semibold tracking-tight">
              {title} is operator-only
            </h1>
            <p className="mt-1 text-sm text-[var(--color-muted-foreground)]">
              This console requires platform-operator scope. Your account
              doesn&apos;t have access to it.
            </p>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
