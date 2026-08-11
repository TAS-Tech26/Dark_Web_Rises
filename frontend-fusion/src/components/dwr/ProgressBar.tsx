import { cn } from "@/lib/utils";

export function ProgressBar({
  value,
  max = 100,
  label,
  className,
  variant = "neon",
}: {
  value: number;
  max?: number;
  label?: string;
  className?: string;
  variant?: "neon" | "magenta";
}) {
  const pct = Math.min(100, Math.max(0, (value / max) * 100));
  return (
    <div className={cn("w-full", className)}>
      {label && (
        <div className="mb-1 flex justify-between font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          <span>{label}</span>
          <span>{Math.round(pct)}%</span>
        </div>
      )}
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-elevated border border-border">
        <div
          className={cn(
            "h-full transition-all duration-500",
            variant === "neon" ? "bg-neon" : "bg-magenta",
          )}
          style={{
            width: `${pct}%`,
            boxShadow:
              variant === "neon"
                ? "0 0 12px color-mix(in oklab, var(--neon) 60%, transparent)"
                : "0 0 12px color-mix(in oklab, var(--magenta) 60%, transparent)",
          }}
        />
      </div>
    </div>
  );
}

/** Segmented step-progress — used for Round 1's 5-image-set indicator + intra-set player passes */
export function StepProgress({
  total,
  current,
  label,
  className,
}: {
  total: number;
  current: number; // 1-indexed
  label?: string;
  className?: string;
}) {
  return (
    <div className={cn("w-full", className)}>
      {label && (
        <div className="mb-1 flex justify-between font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          <span>{label}</span>
          <span>
            {current} / {total}
          </span>
        </div>
      )}
      <div className="flex gap-1.5">
        {Array.from({ length: total }, (_, i) => {
          const active = i < current;
          const now = i === current - 1;
          return (
            <div
              key={i}
              className={cn(
                "h-1.5 flex-1 rounded-full transition-all",
                active ? "bg-neon" : "bg-surface-elevated border border-border",
                now && "shadow-[0_0_12px_color-mix(in_oklab,var(--neon)_70%,transparent)]",
              )}
            />
          );
        })}
      </div>
    </div>
  );
}
