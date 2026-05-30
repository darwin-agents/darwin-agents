"use client";

import { UserButton, useUser } from "@clerk/nextjs";
import { CLERK_ENABLED } from "@/lib/auth-mode";

/**
 * Sidebar account chip. Shows Clerk's <UserButton> + signed-in email when
 * Clerk is wired; renders nothing in dev-token mode (the floating dev chip in
 * <DevAuthProvider> handles sign-out there).
 */
export function AuthChip() {
  if (!CLERK_ENABLED) return null;
  return <ClerkChip />;
}

function ClerkChip() {
  const { user, isLoaded } = useUser();
  if (!isLoaded) return null;
  const label =
    user?.primaryEmailAddress?.emailAddress ??
    user?.username ??
    user?.id ??
    "Signed in";
  return (
    <div className="flex items-center gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-background)] px-2 py-1.5">
      <UserButton afterSignOutUrl="/sign-in" />
      <span className="truncate text-xs text-[var(--color-muted-foreground)]">
        {label}
      </span>
    </div>
  );
}
