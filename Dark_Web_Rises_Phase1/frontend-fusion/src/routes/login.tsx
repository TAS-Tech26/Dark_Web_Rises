import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useState, useEffect } from "react";
import { TopNav } from "@/components/dwr/TopNav";
import { Panel } from "@/components/dwr/Panel";
import { DwrButton } from "@/components/dwr/DwrButton";
import { DwrInput, Field } from "@/components/dwr/Form";
import { useGame } from "@/lib/game-connection";
import { GameState, TeamState } from "@/lib/dwr-protocol";
import {
  ArrowLeft,
  ArrowRight,
  Users,
  Eye,
  EyeOff,
  Loader2,
} from "lucide-react";

export const Route = createFileRoute("/login")({
  component: TeamLogin,
});

function TeamLogin() {
  const navigate = useNavigate();
  const game = useGame();

  const [id, setId] = useState("");
  const [pw, setPw] = useState("");

  const [showPassword, setShowPassword] = useState(false);
  const loading = game.loggingIn;
  const error = game.loginError ?? "";

  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  // Backend accepted the credentials — route by where the game actually is,
  // not unconditionally to the lobby. Logging in after the event has ended (a
  // reconnect, a discarded tab) used to land the player in the waiting room
  // reading "waiting for the administrator to begin Round 1", with no way out.
  // Branch on the GAME being over and on THIS TEAM's state -- never on
  // gameState === GAME_RUNNING alone. A team that connects mid-event has not
  // been admitted yet (admission happens at the next round boundary), so
  // "the game is running" says nothing about where this player belongs. They
  // go to the lobby, which now explains that they will join next round.
  useEffect(() => {
    if (game.userId === null) return;
    if (game.gameState === GameState.GAME_OVER) {
      navigate({ to: "/results" });
    } else if (game.teamState === TeamState.PLAYING) {
      navigate({ to: "/round-1" });
    } else {
      navigate({ to: "/waiting-room" });
    }
  }, [game.userId, game.gameState, game.teamState, navigate]);


  return (
    <div className="min-h-screen">
      <TopNav />

      {/* Background Glow */}
      <div className="pointer-events-none absolute left-1/2 top-56 h-80 w-80 -translate-x-1/2 rounded-full bg-neon/10 blur-3xl" />

      <main className="relative mx-auto flex min-h-[calc(100vh-64px)] max-w-md flex-col justify-center px-4 py-12">
        <div className="pointer-events-none absolute inset-0 bg-grid opacity-30" />

        {/* Back Button */}
        <button
          onClick={() => navigate({ to: "/" })}
          className="mb-6 flex items-center gap-2 font-mono text-xs uppercase tracking-widest text-muted-foreground transition-colors hover:text-neon"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to Home
        </button>

        {/* Login Card */}
        <Panel
          variant="elevated"
          glow="neon"
          className={`relative p-8 transition-all duration-700 ${
            mounted
              ? "translate-y-0 opacity-100"
              : "translate-y-6 opacity-0"
          }`}
        >
          {/* Header */}
          <div className="mb-2 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
            <Users className="h-3 w-3 text-neon" />
            Team Login
          </div>

          <h1 className="mb-2 font-mono text-3xl font-black uppercase tracking-tight text-foreground">
            Access <span className="text-neon">Dark Web Rises</span>
          </h1>

          <p className="mb-8 text-sm text-muted-foreground">
            Enter your assigned Team ID and Password to participate in the
            event.
          </p>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              game.login(id.trim(), pw);
            }}
            className="space-y-5"
          >
            <Field label="Team ID">
              <DwrInput
                value={id}
                onChange={(e) => setId(e.target.value)}
                placeholder="TEAM-001"
                autoComplete="username"
                required
              />
            </Field>

            <Field label="Password">
              <div className="relative">
                <DwrInput
                  type={showPassword ? "text" : "password"}
                  value={pw}
                  onChange={(e) => setPw(e.target.value)}
                  placeholder="Enter Password"
                  autoComplete="current-password"
                  required
                  className="pr-12"
                />

                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground transition-colors hover:text-neon"
                >
                  {showPassword ? (
                    <EyeOff className="h-4 w-4" />
                  ) : (
                    <Eye className="h-4 w-4" />
                  )}
                </button>
              </div>
            </Field>
                        <DwrButton
              type="submit"
              size="lg"
              className="w-full"
              disabled={loading || !game.connected}
              icon={
                loading ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <ArrowRight className="h-4 w-4" />
                )
              }
            >
              {loading
                ? "Authenticating..."
                : game.connected
                  ? "Login"
                  : "Connecting to server..."}
            </DwrButton>

            {(error || game.socketError) && (
              <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-center text-sm text-destructive">
                {error || game.socketError}
              </div>
            )}
          </form>

          {/* Event Status */}
          <div className="mt-8 rounded-lg border border-neon/20 bg-surface/50 p-4 transition-colors hover:border-neon/40">
            <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
              Event Status
            </div>

            <div className="mt-3 flex items-center gap-3">
              <span
                className={`h-2.5 w-2.5 animate-pulse rounded-full ${
                  game.connected ? "bg-neon" : "bg-destructive"
                }`}
              />

              <div>
                <div
                  className={`font-mono text-sm font-semibold ${
                    game.connected ? "text-neon" : "text-destructive"
                  }`}
                >
                  {game.connected
                    ? "Authentication Gateway Online"
                    : "Reconnecting to Gateway..."}
                </div>

                <div className="mt-1 text-xs text-muted-foreground">
                  Waiting for Round 1 to begin.
                </div>
              </div>
            </div>
          </div>


          {/* Help Section */}
          <div className="mt-6 rounded-lg border border-border bg-background/40 p-4">
            <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
              Need Assistance?
            </div>

            <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
              If you are unable to log in, please contact any event volunteer or
              visit the registration desk for assistance.
            </p>
          </div>
                    {/* Footer */}
          <div className="mt-8 border-t border-border pt-5 text-center">
            <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
              Official Event Portal
            </div>

            <div className="mt-2 font-mono text-xs uppercase tracking-widest text-foreground">
              DARK WEB RISES
            </div>

            <div className="mt-1 text-[11px] text-muted-foreground">
              TAMS 2026 • PES University
            </div>
          </div>
        </Panel>
      </main>
    </div>
  );
}