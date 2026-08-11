import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { TopNav } from "@/components/dwr/TopNav";
import { Panel } from "@/components/dwr/Panel";
import { DwrButton } from "@/components/dwr/DwrButton";
import { DwrTextarea, Field } from "@/components/dwr/Form";
import { ImageViewer } from "@/components/dwr/ImageViewer";
import { Send, Loader2, Sparkles, Users, Clock } from "lucide-react";
import { useGame, useSecondsUntil } from "@/lib/game-connection";
import { GameState, TOTAL_ROUNDS, TeamState } from "@/lib/dwr-protocol";
import { toImageSrc } from "@/lib/image-src";

export const Route = createFileRoute("/round-1")({
  component: Round1,
});

function Round1() {
  const navigate = useNavigate();
  const game = useGame();

  const [prompt, setPrompt] = useState("");
  const [confirmed, setConfirmed] = useState(false);

  const secondsLeft = useSecondsUntil(game.turnEndsAt);
  const timeExpired = game.turnEndsAt !== null && secondsLeft === 0;

  const submitted = game.promptPhase === "sending" || game.promptPhase === "accepted";
  const locked = submitted || timeExpired || !game.isMyTurn;

  const imageSrc = useMemo(() => toImageSrc(game.image), [game.image]);

  // Reset the editor whenever a new image / turn arrives.
  useEffect(() => {
    if (game.promptPhase === "idle") {
      setPrompt("");
      setConfirmed(false);
    }
  }, [game.promptPhase, game.image]);

  // Route on lifecycle transitions driven by the server.
  useEffect(() => {
    if (game.userId === null && game.connected && !game.loggingIn) {
      navigate({ to: "/login" });
    } else if (game.gameState === GameState.GAME_OVER || game.teamState === TeamState.DONE) {
      navigate({ to: "/results" });
    } else if (game.gameState === GameState.ROUND_OVER) {
      navigate({ to: "/round-1-results" });
    }
  }, [
    game.userId,
    game.connected,
    game.loggingIn,
    game.gameState,
    game.teamState,
    navigate,
  ]);

  const roundLabel = String(Math.max(1, game.currentRound + 1)).padStart(2, "0");

  return (
    <div className="min-h-screen">
      <TopNav
        teamName={game.username ?? "—"}
        teamId={game.userId !== null ? `PLAYER-${game.userId}` : ""}
        status="live"
      />
      <main className="relative mx-auto max-w-7xl px-6 py-8">
        <div className="pointer-events-none fixed inset-0 bg-grid opacity-20" />

        <div className="relative">
          {/* Header */}
          <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
            <div>
              <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
                // round {roundLabel}
              </div>
              <h1 className="font-mono text-3xl font-black uppercase tracking-tight text-foreground sm:text-4xl">
                Lost In <span className="text-neon">Translation</span>
              </h1>
            </div>

            <Panel className="px-6 py-4 text-center" glow={secondsLeft <= 10 ? "magenta" : "neon"}>
              <div className="flex items-center justify-center gap-2 font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
                <Clock className="h-3 w-3" /> Turn Timer
              </div>
              <div
                className={`mt-1 font-mono text-4xl font-black tabular-nums ${
                  secondsLeft <= 10 ? "text-magenta" : "text-neon"
                }`}
              >
                {String(Math.floor(secondsLeft / 60)).padStart(2, "0")}:
                {String(secondsLeft % 60).padStart(2, "0")}
              </div>
            </Panel>
          </div>

          <Panel className="mb-6 p-4">
            <div className="grid gap-4 md:grid-cols-4">
              <div>
                <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
                  EVENT STATUS
                </div>
                <div
                  className={`mt-1 font-mono text-lg font-bold ${
                    game.connected ? "animate-pulse text-neon" : "text-destructive"
                  }`}
                >
                  {game.connected ? "● LIVE" : "● RECONNECTING"}
                </div>
              </div>

              <div>
                <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
                  ROUND
                </div>
                <div className="mt-1 font-mono text-lg font-bold text-foreground">
                  {roundLabel} / {TOTAL_ROUNDS}
                </div>
              </div>

              <div>
                <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
                  TURN
                </div>
                <div className="mt-1 font-mono text-lg font-bold text-foreground">
                  {game.isMyTurn ? "Yours" : "Teammate"}
                </div>
              </div>

              <div>
                <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
                  TEAM SCORE
                </div>
                <div className="mt-1 font-mono text-lg font-bold text-magenta">
                  {game.teamScore ?? 0}
                </div>
              </div>
            </div>
          </Panel>

          {/* Main split */}
          <div className="grid gap-8 lg:grid-cols-[1.5fr_1fr]">
            {/* Left — image from the server */}
            <div className="flex flex-col gap-4">
              <ImageViewer
                src={imageSrc}
                alt="Image to describe"
                label={game.isMyTurn ? "Describe this image" : "Current image"}
                placeholder="// awaiting image from server"
                aspect="square"
              />
            </div>

            {/* Right — action */}
            <div className="flex flex-col gap-4">
              <Panel glow="neon" className="p-5">
                <div className="flex items-center justify-between">
                  <div>
                    <div className="font-mono text-[10px] uppercase tracking-[0.35em] text-muted-foreground">
                      Active Player
                    </div>
                    <div className="mt-1 font-mono text-2xl font-bold text-neon">
                      {game.isMyTurn ? (game.username ?? "You") : "Teammate"}
                    </div>
                    <div className="font-mono text-[11px] uppercase tracking-widest text-muted-foreground">
                      {game.isMyTurn ? "It is your turn" : "Waiting for their prompt"}
                    </div>
                  </div>

                  <div className="rounded border border-neon/30 bg-neon/10 px-4 py-2 text-center">
                    <div className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                      <Users className="h-3 w-3" /> Online
                    </div>
                    <div className="mt-1 font-mono text-xl font-bold text-neon">
                      {game.connectedMembers}
                    </div>
                  </div>
                </div>

                <div className="mt-4 rounded border border-magenta/30 bg-magenta/5 p-4">
                  <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-magenta">
                    Turn Rules
                  </div>
                  <ul className="mt-3 space-y-2 text-sm text-muted-foreground">
                    <li>• You may submit only one prompt.</li>
                    <li>• Carefully review before confirming.</li>
                    <li>• Prompt cannot be edited after confirmation.</li>
                    <li>• The timer is controlled by the server.</li>
                  </ul>
                </div>
              </Panel>

              {/* Spectator */}
              {!game.isMyTurn && game.promptPhase !== "accepted" && (
                <Panel variant="elevated" className="flex flex-col items-center gap-4 p-10 text-center">
                  <Loader2 className="h-10 w-10 animate-spin text-neon" />
                  <div className="font-mono text-sm uppercase tracking-[0.3em] text-neon">
                    teammate is prompting_
                  </div>
                  <div className="font-mono text-xs text-muted-foreground">
                    Hand over the laptop if the active player is next to you.
                  </div>
                </Panel>
              )}

              {/* Prompt submitted, waiting for generation */}
              {game.promptPhase === "sending" && (
                <Panel variant="elevated" className="flex flex-col items-center gap-4 p-10">
                  <Loader2 className="h-10 w-10 animate-spin text-neon" />
                  <div className="font-mono text-sm uppercase tracking-[0.3em] text-neon animate-pulse">
                    transmitting prompt_
                  </div>
                </Panel>
              )}

              {game.promptPhase === "accepted" && (
                <Panel variant="elevated" className="p-5" glow="magenta">
                  <div className="mb-3 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.3em] text-magenta">
                    <Sparkles className="h-3 w-3" /> prompt accepted
                  </div>
                  <p className="text-sm text-muted-foreground">
                    Your prompt is being rendered. Pass the laptop to the next player — the
                    generated image will appear automatically.
                  </p>
                </Panel>
              )}

              {game.promptPhase === "skipped" && (
                <Panel variant="elevated" className="p-5">
                  <div className="font-mono text-xs uppercase tracking-widest text-warning">
                    Turn skipped — no prompt was submitted in time.
                  </div>
                </Panel>
              )}

              {game.promptPhase === "out_of_chances" && (
                <Panel variant="elevated" className="p-5">
                  <div className="font-mono text-xs uppercase tracking-widest text-destructive">
                    No attempts remaining for this turn.
                  </div>
                </Panel>
              )}

              {/* Prompt editor */}
              {game.isMyTurn && !submitted && game.promptPhase !== "out_of_chances" && (
                <Panel variant="elevated" className="p-5">
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      game.sendPrompt(prompt.trim());
                    }}
                  >
                    <Field
                      label="Your Prompt"
                      hint="Only ONE prompt submission is allowed for this turn."
                    >
                      <DwrTextarea
                        rows={12}
                        placeholder="Describe the image as accurately as possible..."
                        value={prompt}
                        disabled={locked}
                        onChange={(e) => setPrompt(e.target.value)}
                        required
                      />
                    </Field>

                    {game.promptPhase === "invalid" && (
                      <div className="mt-4 rounded border border-destructive/40 bg-destructive/10 p-3 text-center font-mono text-xs uppercase tracking-widest text-destructive">
                        Prompt rejected · {game.attemptsLeft} attempt
                        {game.attemptsLeft === 1 ? "" : "s"} left
                      </div>
                    )}

                    <div className="mt-5 space-y-4">
                      <div className="flex items-center justify-between">
                        <div className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                          {prompt.length} Characters
                        </div>

                        {!confirmed ? (
                          <DwrButton
                            type="button"
                            variant="secondary"
                            disabled={!prompt.trim() || locked}
                            onClick={() => setConfirmed(true)}
                          >
                            Review Prompt
                          </DwrButton>
                        ) : (
                          <span className="font-mono text-xs text-neon">
                            {timeExpired ? "⏱ Time Expired" : "✓ Ready to send"}
                          </span>
                        )}
                      </div>

                      {timeExpired && (
                        <div className="rounded border border-destructive/40 bg-destructive/10 p-3 text-center font-mono text-xs uppercase tracking-widest text-destructive">
                          TIME EXPIRED • WAITING FOR SERVER
                        </div>
                      )}

                      {confirmed && (
                        <Panel glow="magenta" className="border-magenta/40 p-5">
                          <div className="font-mono text-[10px] uppercase tracking-[0.35em] text-magenta">
                            Final Confirmation
                          </div>

                          <p className="mt-3 text-sm text-muted-foreground">
                            You only get{" "}
                            <span className="font-semibold text-neon">ONE</span> prompt
                            submission. Please verify your prompt before continuing.
                          </p>

                          <div className="mt-4 rounded border border-border bg-background/50 p-4">
                            <div className="mb-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                              Prompt Preview
                            </div>
                            <p className="whitespace-pre-wrap text-sm text-foreground">{prompt}</p>
                          </div>

                          <div className="mt-5 flex justify-end gap-3">
                            <DwrButton
                              type="button"
                              variant="ghost"
                              disabled={locked}
                              onClick={() => setConfirmed(false)}
                            >
                              Edit Prompt
                            </DwrButton>

                            <DwrButton
                              type="submit"
                              disabled={locked}
                              icon={<Send className="h-4 w-4" />}
                            >
                              Confirm &amp; Generate
                            </DwrButton>
                          </div>
                        </Panel>
                      )}
                    </div>
                  </form>
                </Panel>
              )}
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
