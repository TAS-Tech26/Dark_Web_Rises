import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { TopNav } from "@/components/dwr/TopNav";
import { Panel } from "@/components/dwr/Panel";
import { StatusPill } from "@/components/dwr/StatusPill";
import { useGame, useSecondsUntil } from "@/lib/game-connection";
import { GameState, TeamState } from "@/lib/dwr-protocol";
import { useEffect } from "react";
import {
  Users,
  ShieldCheck,
  Monitor,
  CheckCircle2,
  Circle,
} from "lucide-react";
import { DwrButton } from "@/components/dwr/DwrButton";
import { ArrowLeft } from "lucide-react";

export const Route = createFileRoute("/waiting-room")({
  component: WaitingRoom,
});

function WaitingRoom() {
  const navigate = useNavigate();
  const game = useGame();
  const countdown = useSecondsUntil(game.countdownEndsAt);

  const authenticated = game.userId !== null;
  const roundStarted =
    game.teamState === TeamState.PLAYING || game.gameState === GameState.GAME_RUNNING;

  // Not logged in (e.g. deep link or dropped session) — back to the gateway.
  useEffect(() => {
    if (!authenticated && !game.loggingIn && game.connected) {
      navigate({ to: "/login" });
    }
  }, [authenticated, game.loggingIn, game.connected, navigate]);

  useEffect(() => {
    if (roundStarted) {
      navigate({ to: "/round-1" });
    }
  }, [roundStarted, navigate]);

  const members = game.roster.length > 0 ? game.roster : game.username ? [game.username] : [];

  return (
    <div className="min-h-screen">
      <TopNav
        teamName={game.username ?? "—"}
        teamId={game.userId !== null ? `PLAYER-${game.userId}` : ""}
        status="waiting"
      />


      <main className="relative mx-auto max-w-6xl px-6 py-10">

        <div className="pointer-events-none fixed inset-0 bg-grid opacity-20" />

        <div className="pointer-events-none absolute left-1/2 top-44 h-96 w-96 -translate-x-1/2 rounded-full bg-neon/10 blur-3xl" />

        <div className="relative">

          <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.35em] text-muted-foreground">
            ROUND 01 • STANDBY
          </div>

          <h1 className="font-mono text-4xl font-black uppercase tracking-tight text-foreground sm:text-5xl">
            Preparing for <span className="text-neon">Round 1</span>
          </h1>

          <p className="mt-3 max-w-3xl text-muted-foreground">
            Your team has been authenticated successfully. Please remain
            seated and wait for the event host to begin Round 1.
            Once the administrator starts the event,
            you will automatically proceed into the competition.
          </p>

          <div className="mt-10 grid gap-5 lg:grid-cols-[2fr_1fr]">
                      {/* LEFT COLUMN */}
          <div className="space-y-5">

            {/* Team Information */}
            <Panel className="p-6">

              <div className="mb-5 flex items-center justify-between">

                <div>

                  <div className="font-mono text-[10px] uppercase tracking-[0.35em] text-muted-foreground">
                    Team Information
                  </div>

                  <div className="mt-2 font-mono text-3xl font-bold text-neon">
                    {game.username ?? "—"}
                  </div>

                  <div className="font-mono text-xs text-muted-foreground">
                    {game.connectedMembers} connected
                  </div>


                </div>

                <StatusPill status="waiting" label="Authenticated" />

              </div>

              <div className="grid gap-4 md:grid-cols-2">

                <div className="rounded-lg border border-border bg-surface/40 p-4">

                  <div className="mb-3 flex items-center gap-2 font-mono text-xs uppercase tracking-widest text-neon">
                    <Users className="h-4 w-4" />
                    Player Order
                  </div>

                  <div className="space-y-3">

                    {members.length === 0 && (
                      <div className="rounded border border-border bg-background/40 p-3 text-sm text-muted-foreground">
                        Waiting for teammates to connect...
                      </div>
                    )}

                    {members.map((member, index) => (

                      <div
                        key={member}
                        className="flex items-center gap-3 rounded border border-border bg-background/40 p-3"
                      >

                        <div className="flex h-8 w-8 items-center justify-center rounded-full border border-neon/40 bg-neon/10 font-mono font-bold text-neon">
                          {index + 1}
                        </div>

                        <div>

                          <div className="font-mono text-sm font-semibold text-foreground">
                            {member}
                            {member === game.username && (
                              <span className="ml-2 text-[10px] uppercase tracking-widest text-neon">
                                You
                              </span>
                            )}
                          </div>

                          <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                            Player {index + 1}
                          </div>

                        </div>

                      </div>

                    ))}


                  </div>

                </div>

                <div className="rounded-lg border border-border bg-surface/40 p-4">

                  <div className="mb-3 flex items-center gap-2 font-mono text-xs uppercase tracking-widest text-neon">
                    <ShieldCheck className="h-4 w-4" />
                    Team Status
                  </div>

                  <div className="space-y-4">

                    <Row
                      label="Authentication"
                      value={
                        <span className="font-mono text-neon">
                          Complete
                        </span>
                      }
                    />

                    <Row
                      label="Round"
                      value={
                        <span className="font-mono">
                          Round 01
                        </span>
                      }
                    />

                    <Row
                      label="Mode"
                      value={
                        <span className="font-mono">
                          Standby
                        </span>
                      }
                    />

                    <Row
                      label="Device"
                      value={
                        <span className="font-mono">
                          One Laptop
                        </span>
                      }
                    />

                  </div>

                </div>

              </div>

            </Panel>

            {/* Round Overview */}

            <Panel className="p-6">

              <div className="mb-5 font-mono text-[10px] uppercase tracking-[0.35em] text-muted-foreground">
                Round Overview
              </div>

              <h2 className="font-mono text-2xl font-bold text-neon">
                Lost In Translation
              </h2>

              <p className="mt-3 text-muted-foreground">
                Your team will work together on a single laptop.
                Each player receives the image generated by the previous player,
                writes a new prompt, and passes the laptop to the next teammate.
              </p>

              <div className="mt-6 grid gap-4 sm:grid-cols-4">

                <div className="rounded border border-border bg-surface/40 p-4 text-center">
                  <div className="font-mono text-3xl font-black text-neon">
                    5
                  </div>
                  <div className="mt-1 text-xs uppercase tracking-widest text-muted-foreground">
                    Sets
                  </div>
                </div>

                <div className="rounded border border-border bg-surface/40 p-4 text-center">
                  <div className="font-mono text-3xl font-black text-neon">
                    4
                  </div>
                  <div className="mt-1 text-xs uppercase tracking-widest text-muted-foreground">
                    Players
                  </div>
                </div>

                <div className="rounded border border-border bg-surface/40 p-4 text-center">
                  <div className="font-mono text-3xl font-black text-neon">
                    90s
                  </div>
                  <div className="mt-1 text-xs uppercase tracking-widest text-muted-foreground">
                    Per Turn
                  </div>
                </div>

                <div className="rounded border border-border bg-surface/40 p-4 text-center">
                  <div className="font-mono text-3xl font-black text-neon">
                    1
                  </div>
                  <div className="mt-1 text-xs uppercase tracking-widest text-muted-foreground">
                    Laptop
                  </div>
                </div>

              </div>

            </Panel>

          </div>

          {/* RIGHT COLUMN */}
          <div className="space-y-5">
                        {/* Rules */}
            <Panel className="p-6">

              <div className="mb-4 font-mono text-[10px] uppercase tracking-[0.35em] text-muted-foreground">
                Event Instructions
              </div>

              <div className="space-y-4">

                <Rule
                  title="One Laptop Per Team"
                  desc="All four players will use the same system throughout Round 1."
                />

                <Rule
                  title="One Player At A Time"
                  desc="Only the current player should interact with the laptop."
                />

                <Rule
                  title="Pass The Laptop"
                  desc="After your turn ends, immediately hand the laptop to the next teammate."
                />

                <Rule
                  title="Timer Starts On BEGIN"
                  desc="The countdown only begins once the current player presses BEGIN."
                />

                <Rule
                  title="Do Not Refresh"
                  desc="Remain on this page until instructed otherwise."
                />

              </div>

            </Panel>

            {/* System Status */}

            <Panel className="p-6">

              <div className="mb-4 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.35em] text-muted-foreground">
                <Monitor className="h-4 w-4 text-neon" />
                System Status
              </div>

              <div className="space-y-4">

                <Row
                  label="Authentication"
                  value={
                    <span className="font-mono text-neon">
                      {authenticated ? "Complete" : "Pending"}
                    </span>
                  }
                />

                <Row
                  label="Backend"
                  value={
                    <span
                      className={`font-mono ${game.connected ? "text-neon" : "text-destructive"}`}
                    >
                      {game.connected ? "Connected" : "Reconnecting"}
                    </span>
                  }
                />

                <Row
                  label="Round Status"
                  value={
                    <span
                      className={`font-mono ${
                        game.gameState === GameState.COUNTDOWN ? "text-neon" : "text-warning"
                      }`}
                    >
                      {game.gameState === GameState.COUNTDOWN ? "Starting" : "Waiting"}
                    </span>
                  }
                />

                <Row
                  label="Players Online"
                  value={
                    <span className="font-mono text-neon">
                      {game.connectedMembers}
                    </span>
                  }
                />

              </div>

            </Panel>

            {/* TEAM READY */}

            <Panel
              glow="neon"
              className="border-neon/30 p-8 text-center"
            >

              <div className="mb-3 flex justify-center">
                <CheckCircle2 className="h-12 w-12 text-neon" />
              </div>

              <div className="font-mono text-xl font-black uppercase tracking-widest text-neon">
                TEAM READY
              </div>

              {game.gameState === GameState.COUNTDOWN ? (
                <>
                  <p className="mt-4 text-sm text-muted-foreground">
                    Round 1 begins in
                  </p>
                  <div className="mt-2 font-mono text-6xl font-black tabular-nums text-neon">
                    {countdown}
                  </div>
                </>
              ) : (
                <p className="mt-4 text-sm text-muted-foreground">
                  Waiting for the event administrator to begin Round 1.
                </p>
              )}

              <div className="mt-6 flex items-center justify-center gap-2 font-mono text-xs uppercase tracking-[0.3em] text-neon">

                <span className="h-2 w-2 animate-pulse rounded-full bg-neon" />

                System Standing By

              </div>
              {!roundStarted && game.gameState !== GameState.COUNTDOWN && (
                <DwrButton
                  variant="ghost"
                  className="mt-6 w-full"
                  icon={<ArrowLeft className="h-4 w-4" />}
                  onClick={() => {
                    game.logout();
                    navigate({ to: "/" });
                  }}
                >
                  Exit Lobby
                </DwrButton>
              )}


            </Panel>

          </div>
          
          </div>

        </div>

        {/* Footer */}
        <div className="mt-10 border-t border-border pt-6 text-center">

          <div className="font-mono text-[10px] uppercase tracking-[0.35em] text-muted-foreground">
            DARK WEB RISES
          </div>

          <div className="mt-2 font-mono text-lg font-bold text-neon">
            Awaiting Administrator
          </div>

          <p className="mx-auto mt-4 max-w-2xl text-sm text-muted-foreground">
            Once the administrator starts Round 1, your team will be
            automatically redirected into the competition.
            Please remain seated and do not refresh or close this page.
          </p>

        </div>

      </main>
    </div>
  );
}

function Row({
  label,
  value,
}: {
  label: string;
  value: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between border-b border-border/50 pb-2 last:border-none">

      <span className="font-mono text-[11px] uppercase tracking-widest text-muted-foreground">
        {label}
      </span>

      {value}

    </div>
  );
}

function Rule({
  title,
  desc,
}: {
  title: string;
  desc: string;
}) {
  return (
    <div className="flex gap-3">

      <Circle
        className="mt-1 h-3 w-3 shrink-0 text-neon"
        fill="currentColor"
      />

      <div>

        <div className="font-mono text-sm font-semibold text-foreground">
          {title}
        </div>

        <div className="mt-1 text-sm text-muted-foreground">
          {desc}
        </div>

      </div>

    </div>
  );
}