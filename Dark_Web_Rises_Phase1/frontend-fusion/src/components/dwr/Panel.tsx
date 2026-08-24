import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/** Reusable branded panel — the primary card surface for Dark Web Rises */
export function Panel({
  children,
  className,
  variant = "default",
  glow,
}: {
  children: ReactNode;
  className?: string;
  variant?: "default" | "elevated";
  glow?: "neon" | "magenta" | "none";
}) {
  return (
    <div
      className={cn(
        "relative rounded-lg",
        variant === "elevated" ? "panel-elevated" : "panel",
        glow === "neon" && "glow-neon",
        glow === "magenta" && "glow-magenta",
        className,
      )}
    >
      {/* corner brackets */}
      <span className="pointer-events-none absolute -top-px -left-px h-3 w-3 border-t border-l border-neon" />
      <span className="pointer-events-none absolute -top-px -right-px h-3 w-3 border-t border-r border-neon" />
      <span className="pointer-events-none absolute -bottom-px -left-px h-3 w-3 border-b border-l border-neon" />
      <span className="pointer-events-none absolute -bottom-px -right-px h-3 w-3 border-b border-r border-neon" />
      {children}
    </div>
  );
}
