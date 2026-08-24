import { cn } from "@/lib/utils";

type Status = "online" | "offline" | "waiting" | "live" | "paused" | "ended";

const map: Record<Status, { color: string; label: string }> = {
  online: { color: "bg-success", label: "Online" },
  offline: { color: "bg-destructive", label: "Offline" },
  waiting: { color: "bg-warning", label: "Waiting" },
  live: { color: "bg-neon", label: "Live" },
  paused: { color: "bg-warning", label: "Paused" },
  ended: { color: "bg-muted-foreground", label: "Ended" },
};

export function StatusPill({
  status,
  label,
  className,
}: {
  status: Status;
  label?: string;
  className?: string;
}) {
  const m = map[status];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-2 rounded-full border border-border bg-surface/70 px-3 py-1 font-mono text-[10px] uppercase tracking-[0.25em] text-foreground/90",
        className,
      )}
    >
      <span className={cn("relative flex h-2 w-2")}>
        <span className={cn("absolute inline-flex h-full w-full rounded-full opacity-70 animate-ping", m.color)} />
        <span className={cn("relative inline-flex h-2 w-2 rounded-full", m.color)} />
      </span>
      {label ?? m.label}
    </span>
  );
}
