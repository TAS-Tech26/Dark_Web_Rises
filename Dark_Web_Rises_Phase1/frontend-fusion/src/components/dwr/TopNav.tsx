import { Link, useRouterState } from "@tanstack/react-router";
import { cn } from "@/lib/utils";
import { StatusPill } from "./StatusPill";

export function BrandMark({ className }: { className?: string }) {
  return (
    <div className={cn("flex items-center gap-4", className)}>
      {/* PES Logo */}
      <img
        src="/logos/pes logo.png"
        alt="PES University"
        className="h-10 w-auto object-contain"
      />

      {/* Event Title */}
      <div className="flex flex-col leading-none">
        <span className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
          TAMS 2026
        </span>

        <span className="font-mono text-sm font-bold uppercase tracking-widest text-foreground">
          Dark Web <span className="text-neon">Rises</span>
        </span>
      </div>
    </div>
  );
}

export function TopNav({
  teamName,
  teamId,
  status = "online",
  className,
}: {
  teamName?: string;
  teamId?: string;
  status?: "online" | "offline" | "waiting" | "live" | "paused" | "ended";
  className?: string;
}) {
  const pathname = useRouterState({
    select: (s) => s.location.pathname,
  });

  const isAuthPage =
    pathname === "/" ||
    pathname === "/login" ||
    pathname.startsWith("/admin");

  return (
    <header
      className={cn(
        "sticky top-0 z-40 flex items-center justify-between border-b border-border bg-background/70 px-4 py-3 backdrop-blur-lg sm:px-8",
        className
      )}
    >
      {/* Left Side */}
      <BrandMark />

      {/* Right Side */}
      <div className="flex items-center gap-5">
        {!isAuthPage && teamName && (
          <div className="hidden text-right sm:block">
            <div className="font-mono text-xs font-semibold text-foreground">
              {teamName}
            </div>

            <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
              {teamId}
            </div>
          </div>
        )}

        <StatusPill status={status} />

        {/* TAMS Logo */}
        <img
          src="/logos/tams logo.png"
          alt="TAMS"
          className="h-10 w-auto object-contain"
        />
      </div>
    </header>
  );
}