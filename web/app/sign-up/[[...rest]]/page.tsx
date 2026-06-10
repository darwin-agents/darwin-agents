import { SignUp } from "@clerk/nextjs";
import { Activity } from "lucide-react";

export default function Page() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-6 px-4">
      <div className="flex items-center gap-2">
        <Activity className="size-6 text-[var(--color-status-alive)]" />
        <span className="text-lg font-semibold tracking-tight">Darwin</span>
      </div>
      {/* Routing + redirect targets come from the NEXT_PUBLIC_CLERK_* env
          vars; the dark theme is inherited from ClerkProvider. */}
      <SignUp />
    </div>
  );
}
