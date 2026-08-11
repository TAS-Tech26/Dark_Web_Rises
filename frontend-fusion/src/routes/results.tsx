import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect } from "react";
import { TopNav } from "@/components/dwr/TopNav";
import { Panel } from "@/components/dwr/Panel";
import { ScoreCard } from "@/components/dwr/ScoreCard";
import { DwrButton } from "@/components/dwr/DwrButton";
import { useGame } from "@/lib/game-connection";
import { TOTAL_ROUNDS } from "@/lib/dwr-protocol";
import { ExternalLink, LogOut, Trophy } from "lucide-react";

export const Route = createFileRoute("/results")({
  component: FinalResults,
});

const CTFD_URL = import.meta.env.VITE_CTFD_URL as string | undefined;

function FinalResults() {
  const navigate = useNavigate();
  const game = useGame();

  useEffect(() => {
    if (game.userId === null && game.connected && !game.loggingIn) {
      navigate({ to: "/login" });
    }
  }, [game.userId, game.connected, game.loggingIn, navigate]);

  const podium = game.top3 ?? [];
  const perRound = game.teamScores ?? game.rounds.map((r) => r.score);

  return (
    <div className="min-h-screen">
      <TopNav
        teamName={game.username ?? "—"}
        teamId={game.userId !== null ? `PLAYER-${game.userId}` : ""}
        status="ended"
      />
      <main className="relative mx-auto max-w-6xl px-6 py-10">
        <div className="pointer-events-none fixed inset-0 bg-grid opacity-20" />

        <div className="relative">
          <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
            // event · terminated
          </div>
          <h1 className="mb-8 font-mono text-4xl font-black uppercase tracking-tight text-foreground sm:text-5xl">
            Final <span className="text-neon">Standings</span>
          </h1>

          {/* Podium — top 3 straight from the server */}
          {podium.length > 0 && (
            <div className="mb-8 grid gap-4 sm:grid-cols-3">
              {podium.map(([teamId, score], i) => {
                const heights = ["sm:mt-0", "sm:mt-6", "sm:mt-12"];
                const glow = i === 0 ? "neon" : i === 1 ? undefined : "magenta";
                return (
                  <Panel
                    key={teamId}
                    className={`p-6 text-center ${heights[i]}`}
                    glow={glow as "neon" | "magenta" | undefined}
                  >
                    <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full border border-neon/40 bg-neon/5">
                      <Trophy
                        className={`h-5 w-5 ${
                          i === 0 ? "text-neon" : i === 1 ? "text-foreground" : "text-magenta"
                        }`}
                      />
                    </div>
                    <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
                      Rank #{i + 1}
                    </div>
                    <div className="mt-2 font-mono text-xl font-bold text-foreground">
                      TEAM {String(teamId).padStart(3, "0")}
                    </div>
                    <div className="mt-3 font-mono text-3xl font-black tabular-nums text-neon">
                      {Number(score).toLocaleString()}
                    </div>
                  </Panel>
                );
              })}
            </div>
          )}

          {/* Your team */}
          <div className="mb-6 grid gap-4 md:grid-cols-3">
            <ScoreCard
              label="Your Final Score"
              value={(game.teamScore ?? 0).toLocaleString()}
            />
            <ScoreCard
              label="Your Final Rank"
              value={game.rank !== null ? `#${String(game.rank).padStart(2, "0")}` : "—"}
              variant="magenta"
            />
            <ScoreCard
              label="Rounds Completed"
              value={`${perRound.length} / ${TOTAL_ROUNDS}`}
              variant="muted"
            />
          </div>

          <Panel className="overflow-hidden">
            <div className="border-b border-border px-5 py-3">
              <h3 className="font-mono text-sm uppercase tracking-[0.25em] text-neon">
                Your Round Scores
              </h3>
            </div>
            <div className="divide-y divide-border">
              {perRound.length === 0 && (
                <div className="px-5 py-4 font-mono text-xs text-muted-foreground">
                  // waiting for the server to publish scores
                </div>
              )}
              {perRound.map((score, i) => (
                <div
                  key={i}
                  className="flex items-center justify-between px-5 py-3 font-mono"
                >
                  <span className="text-sm text-muted-foreground">
                    ROUND {String(i + 1).padStart(2, "0")}
                  </span>
                  <span className="text-sm font-bold tabular-nums text-neon">
                    +{Number(score).toLocaleString()}
                  </span>
                </div>
              ))}
            </div>
          </Panel>

          {CTFD_URL && (
            <Panel glow="magenta" className="mt-8 p-6">
              <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
                // round 02 · capture the flag
              </div>
              <h3 className="mt-2 font-mono text-xl font-bold uppercase text-foreground">
                Continue on the <span className="text-magenta">CTF platform</span>
              </h3>
              <p className="mt-2 font-mono text-xs text-muted-foreground">
                Round 2 runs on an external CTFd instance. Use the link below to enter.
              </p>
              <a
                href={CTFD_URL}
                target="_blank"
                rel="noreferrer"
                className="mt-4 inline-block"
              >
                <DwrButton icon={<ExternalLink className="h-4 w-4" />}>Open Round 2</DwrButton>
              </a>
            </Panel>
          )}

          <div className="mt-8 flex justify-end">
            <DwrButton
              variant="ghost"
              icon={<LogOut className="h-4 w-4" />}
              onClick={() => {
                game.logout();
                navigate({ to: "/" });
              }}
            >
              Log Out
            </DwrButton>
          </div>
        </div>
      </main>
    </div>
  );
}
