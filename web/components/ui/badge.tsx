import type { HTMLAttributes } from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/cn";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium transition-colors",
  {
    variants: {
      variant: {
        default:
          "border-[var(--color-border)] bg-[var(--color-muted)] text-[var(--color-foreground)]",
        outline:
          "border-[var(--color-border)] bg-transparent text-[var(--color-muted-foreground)]",
        accent:
          "border-transparent bg-[var(--color-accent)] text-[var(--color-accent-foreground)]",
        destructive:
          "border-transparent bg-[var(--color-destructive)] text-[var(--color-destructive-foreground)]",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps
  extends HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
