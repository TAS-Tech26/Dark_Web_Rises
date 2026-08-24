import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/utils";

type Variant = "primary" | "secondary" | "ghost" | "danger" | "magenta";
type Size = "sm" | "md" | "lg";

const variants: Record<Variant, string> = {
  primary:
    "bg-neon text-neon-foreground hover:bg-neon/90 shadow-[0_0_24px_color-mix(in_oklab,var(--neon)_50%,transparent)] hover:shadow-[0_0_36px_color-mix(in_oklab,var(--neon)_70%,transparent)]",
  secondary:
    "bg-surface-elevated text-foreground border border-border hover:border-neon hover:text-neon",
  ghost:
    "bg-transparent text-muted-foreground hover:text-neon hover:bg-surface/50",
  danger:
    "bg-destructive text-destructive-foreground hover:bg-destructive/90",
  magenta:
    "bg-magenta text-accent-foreground hover:bg-magenta/90 shadow-[0_0_24px_color-mix(in_oklab,var(--magenta)_50%,transparent)]",
};

const sizes: Record<Size, string> = {
  sm: "h-8 px-3 text-xs",
  md: "h-10 px-5 text-sm",
  lg: "h-12 px-8 text-base",
};

export interface DwrButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  icon?: ReactNode;
}

export function DwrButton({
  variant = "primary",
  size = "md",
  icon,
  className,
  children,
  ...rest
}: DwrButtonProps) {
  return (
    <button
      {...rest}
      className={cn(
        "inline-flex items-center justify-center gap-2 rounded font-mono font-semibold uppercase tracking-widest transition-all duration-200 disabled:cursor-not-allowed disabled:opacity-40",
        variants[variant],
        sizes[size],
        className,
      )}
    >
      {icon}
      {children}
    </button>
  );
}
