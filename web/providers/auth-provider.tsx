/**
 * Top-level auth provider — dispatches between Clerk and the dev token-paste
 * flow based on `CLERK_ENABLED`. Centralises the toggle so neither the layout
 * nor downstream components need to know about both paths.
 */
import type { ReactNode } from "react";
import { ClerkProvider } from "@clerk/nextjs";
import { dark } from "@clerk/themes";
import { CLERK_ENABLED } from "@/lib/auth-mode";
import { DevAuthProvider } from "./dev-auth-provider";

export function AuthProvider({ children }: { children: ReactNode }) {
  if (CLERK_ENABLED) {
    return (
      <ClerkProvider
        appearance={{ baseTheme: dark }}
        afterSignOutUrl="/sign-in"
      >
        {children}
      </ClerkProvider>
    );
  }
  return <DevAuthProvider>{children}</DevAuthProvider>;
}
