import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect } from "react";
import { TopNav } from "@/components/dwr/TopNav";
import { Panel } from "@/components/dwr/Panel";
import { ScoreCard } from "@/components/dwr/ScoreCard";
import { ImageViewer } from "@/components/dwr/ImageViewer";
import { useGame, useSecondsUntil } from "@/lib/game-connection";
import { toImageSrc } from "@/lib/image-src";
import { GameState, TeamState } from "@/lib/dwr-protocol";
import { CheckCircle2 } from "lucide-react";

export const Route = createFileRoute("/round-1-results")({
  component: RoundResults,
});

function RoundResults() {
  const navigate = useNavigate();
  const game = useGame();
  const breakSeconds = useSecondsUntil(game.roundBreakEndsAt);

  // The server drives every transition out of this screen.
  useEffect(() => {
    if (game.userId === null && game.connected && !game.loggingIn) {
      navigate({ to: "/login" });
    } else if (game.gameState === GameState.GAME_OVER || game.teamState === TeamState.DONE) {
      navigate({ to: "/results" });
    } else if (game.gameState === GameState.GAME_RUNNING) {
      navigate({ to: "/round-1" });
    }
  }, [game.userId, game.connected, game.loggingIn, game.gameState, game.teamState, navigate]);

  const completedRound = Math.max(1, game.currentRound);
  const isFinalRound = completedRound >= game.totalRounds;
  // The whole team sees this, including the three members who spent the
  // round looking at the "wait your turn" placeholder.
  const finalImage = toImageSrc(game.lastRoundImage);
  const bestRound = game.rounds.reduce<number | null>(
    (best, r) => (best === null || r.score > best ? r.score : best),
    null,
  );

  return (
    <div className="min-h-screen">
      <TopNav
        teamName={game.username ?? "—"}
        teamId={game.userId !== null ? `PLAYER-${game.userId}` : ""}
        status="waiting"
      />
      <main className="relative mx-auto max-w-5xl px-6 py-10">
        <div className="pointer-events-none fixed inset-0 bg-grid opacity-20" />

        <div className="relative">
          <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
            // round {String(completedRound).padStart(2, "0")} · complete
          </div>
          <h1 className="mb-2 font-mono text-4xl font-black uppercase tracking-tight text-foreground sm:text-5xl">
            Round {completedRound} <span className="text-neon">Complete</span>
          </h1>

          <p className="mb-8 max-w-2xl text-muted-foreground">
            {isFinalRound
              ? "That was the final round. Standings are being calculated."
              : "Scores are locked in. The next round starts automatically — stay on this page."}
          </p>

          <div className="grid gap-4 md:grid-cols-3">
            <ScoreCard
              label="Round Score"
              value={(game.lastRoundScore ?? 0).toLocaleString()}
              hint={`Round ${completedRound} of ${game.totalRounds}`}
            />
            <ScoreCard
              label="Team Total"
              value={(game.teamScore ?? 0).toLocaleString()}
              variant="magenta"
            />
            <Panel className="p-5">
              <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
                {isFinalRound ? "Status" : "Next Round In"}
              </div>
              {isFinalRound ? (
                <div className="mt-2 flex items-center gap-2 font-mono text-2xl font-bold uppercase text-success">
                  <CheckCircle2 className="h-6 w-6" />
                  Finished
                </div>
              ) : (
                <div className="mt-2 font-mono text-4xl font-black tabular-nums text-neon">
                  {breakSeconds}s
                </div>
              )}
              <div className="mt-1 text-xs text-muted-foreground">
                {isFinalRound ? "Awaiting final standings" : "Next round begins automatically"}
              </div>
            </Panel>
          </div>

          {bestRound !== null && (
            <div className="mt-6 grid gap-4 md:grid-cols-3">
              <ScoreCard label="Rounds Played" value={game.rounds.length} />
              <ScoreCard label="Best Round" value={bestRound.toLocaleString()} variant="neon" />
              <ScoreCard
                label="Average"
                value={Math.round(
                  game.rounds.reduce((a, r) => a + r.score, 0) / Math.max(1, game.rounds.length),
                ).toLocaleString()}
                variant="muted"
              />
            </div>
          )}

          <Panel className="mt-6 overflow-hidden">
            <div className="border-b border-border px-5 py-3">
              <h3 className="font-mono text-sm uppercase tracking-[0.25em] text-neon">
                Round Breakdown
              </h3>
            </div>
            <div className="divide-y divide-border">
              {game.rounds.length === 0 && (
                <div className="px-5 py-4 font-mono text-xs text-muted-foreground">
                  // no rounds scored yet
                </div>
              )}
              {game.rounds.map((r) => {
                const max = Math.max(1, bestRound ?? 1);
                return (
                  <div
                    key={r.round}
                    className="grid grid-cols-[6rem_1fr_6rem] items-center gap-4 px-5 py-3 font-mono"
                  >
                    <div className="text-sm font-bold text-muted-foreground">
                      ROUND {String(r.round).padStart(2, "0")}
                    </div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-surface-elevated">
                      <div
                        className="h-full bg-neon"
                        style={{
                          width: `${Math.min(100, (r.score / max) * 100)}%`,
                          boxShadow:
                            "0 0 10px color-mix(in oklab, var(--neon) 60%, transparent)",
                        }}
                      />
                    </div>
                    <div className="text-right text-sm font-bold tabular-nums text-neon">
                      +{r.score}
                    </div>
                  </div>
                );
              })}
            </div>
          </Panel>

          {/* Rendered only when there is an image. A panel containing the
              viewer's empty-state placeholder would read as "something is
              loading" on a screen where nothing further is coming. */}
          {finalImage && (
            <Panel className="mt-6 overflow-hidden">
              <div className="border-b border-border px-5 py-3">
                <h3 className="font-mono text-sm uppercase tracking-[0.25em] text-neon">
                  Where Round {completedRound} Ended Up
                </h3>
              </div>
              <div className="px-5 py-6">
                <p className="mb-4 max-w-2xl text-sm text-muted-foreground">
                  The last image in your team&apos;s chain. Your round score is how
                  close this landed to the reference image you started from.
                </p>
                <div className="mx-auto w-full max-w-md">
                  <ImageViewer
                    src={finalImage}
                    alt={`Team's final image for round ${completedRound}`}
                    label={`Round ${String(completedRound).padStart(2, "0")} · final image`}
                  />
                </div>
              </div>
            </Panel>
          )}
        </div>
      </main>
    </div>
  );
}
