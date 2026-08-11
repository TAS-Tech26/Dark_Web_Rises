import { cn } from "@/lib/utils";
import { Panel } from "./Panel";

export function ScoreCard({
  label,
  value,
  hint,
  variant = "neon",
  className,
}: {
  label: string;
  value: string | number;
  hint?: string;
  variant?: "neon" | "magenta" | "muted";
  className?: string;
}) {
  const color =
    variant === "neon" ? "text-neon" : variant === "magenta" ? "text-magenta" : "text-foreground";
  return (
    <Panel className={cn("p-5", className)}>
      <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
        {label}
      </div>
      <div className={cn("mt-2 font-mono text-4xl font-bold tabular-nums", color)}>{value}</div>
      {hint && <div className="mt-1 text-xs text-muted-foreground">{hint}</div>}
    </Panel>
  );
}
