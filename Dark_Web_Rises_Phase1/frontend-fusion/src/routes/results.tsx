import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { TopNav } from "@/components/dwr/TopNav";
import { Panel } from "@/components/dwr/Panel";
import { ScoreCard } from "@/components/dwr/ScoreCard";
import { DwrButton } from "@/components/dwr/DwrButton";
import { useGame } from "@/lib/game-connection";
import { ExternalLink, LogOut, Trophy } from "lucide-react";

export const Route = createFileRoute("/results")({
  component: FinalResults,
});

const CTFD_URL = import.meta.env.VITE_CTFD_URL as string | undefined;

function FinalResults() {
  const navigate = useNavigate();
  const game = useGame();
  const [copied, setCopied] = useState(false);
  // Distinct from `copied`: navigator.clipboard is undefined outside a SECURE
  // context, so on a plain-http venue LAN the old optional-chained call silently
  // no-opped while still reporting "// copied". A player who trusts that leaves
  // the screen believing the Round 2 password is on their clipboard -- and it is
  // the only place they will ever see it.
  const [copyFailed, setCopyFailed] = useState(false);

  useEffect(() => {
    if (game.userId === null && game.connected && !game.loggingIn) {
      navigate({ to: "/login" });
    }
  }, [game.userId, game.connected, game.loggingIn, navigate]);

  const perRound = game.teamScores ?? game.rounds.map((r) => r.score);

  // null while the GAME_OVER frame is still in flight, or if the backend
  // could not compute the cut -- treated as "not yet known", not "rejected".
  const qualified = game.qualified;
  const ctfdUrl = game.phase2Url ?? CTFD_URL;

  // Prefer the named leaderboard the server now sends. `top3` is bare
  // (team_id, score) tuples, which could only ever render "TEAM 001";
  // the leaderboard carries real team names and tie-aware 1-based ranks.
  const podium =
    game.leaderboard?.slice(0, 3).map((entry) => ({
      key: entry.team_id,
      name: entry.team_name || `TEAM ${String(entry.team_id).padStart(3, "0")}`,
      score: entry.score,
      rank: entry.rank,
    })) ??
    (game.top3 ?? []).map(([teamId, score], i) => ({
      key: teamId,
      name: `TEAM ${String(teamId).padStart(3, "0")}`,
      score,
      rank: i + 1,
    }));

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
              {podium.map((entry, i) => {
                const heights = ["sm:mt-0", "sm:mt-6", "sm:mt-12"];
                const glow = i === 0 ? "neon" : i === 1 ? undefined : "magenta";
                return (
                  <Panel
                    key={entry.key}
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
                      Rank #{entry.rank}
                    </div>
                    <div className="mt-2 font-mono text-xl font-bold text-foreground">
                      {entry.name}
                    </div>
                    <div className="mt-3 font-mono text-3xl font-black tabular-nums text-neon">
                      {Number(entry.score).toLocaleString()}
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
              value={`${perRound.length} / ${game.totalRounds}`}
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

          {/* Round 2 handoff. This is the only Round 2 UI in the app --
              CTFd owns the entire CTF experience (challenges, submissions,
              its own scoreboard). All this does is hand the player over.

              Only the top 50% of the final standings advance, so the panel is
              driven by the per-team `qualified` flag on the GAME_OVER frame.
              The server only sends the URL to teams that made the cut;
              VITE_CTFD_URL stays as a build-time fallback.

              Rendered unconditionally rather than behind `ctfdUrl &&`: if the
              link is missing the old version showed nothing at all, so a
              misconfiguration would look identical to a working page and
              nobody would notice until 600 people had finished Round 1 with
              no way forward. */}
          {qualified === false ? (
            <Panel className="mt-8 p-6">
              <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
                // round 02 · access denied
              </div>
              <h3 className="mt-2 font-mono text-xl font-bold uppercase text-foreground">
                Your run ends <span className="text-magenta">here</span>
              </h3>
              <p className="mt-2 font-mono text-xs text-muted-foreground">
                Only the top 50% of teams advance to Round 2, and this team finished
                below the cut. Your Round 1 score is locked in — thanks for playing.
              </p>
            </Panel>
          ) : (
            <Panel glow="magenta" className="mt-8 p-6">
              <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
                // round 02
              </div>
              <h3 className="mt-2 font-mono text-xl font-bold uppercase text-foreground">
                {qualified
                  ? <>You <span className="text-magenta">qualified</span> for Round 2</>
                  : <>Round 2 is <span className="text-magenta">live</span></>}
              </h3>

              {ctfdUrl ? (
                <>
                  <p className="mt-2 font-mono text-xs text-muted-foreground">
                    {qualified
                      ? "Your team finished in the top 50% and is through to Round 2. "
                      : ""}
                    Round 2 runs on a separate platform. Sign in there to start solving.
                    Your Round 1 score is already locked in.
                  </p>

                  {/* Credentials for the CTFd account created for this team.
                      The server only sends these to the team they belong to.
                      Shown in full rather than masked: every member needs to
                      copy them down before leaving this screen, and there is
                      nothing to protect them from here -- the whole team is
                      looking at the same page. */}
                  {game.phase2Password && (
                    <div className="mt-4 rounded border border-magenta/40 bg-magenta/5 p-4">
                      <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
                        // your round 2 login
                      </div>
                      <dl className="mt-3 space-y-2">
                        <div className="flex items-baseline justify-between gap-4">
                          <dt className="font-mono text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
                            Username
                          </dt>
                          <dd className="font-mono text-sm font-bold text-foreground">
                            {game.phase2Username ?? game.username ?? "—"}
                          </dd>
                        </div>
                        <div className="flex items-baseline justify-between gap-4">
                          <dt className="font-mono text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
                            Password
                          </dt>
                          <dd className="select-all break-all font-mono text-sm font-bold tracking-wider text-magenta">
                            {game.phase2Password}
                          </dd>
                        </div>
                      </dl>
                      <button
                        type="button"
                        onClick={async () => {
                          setCopyFailed(false);
                          try {
                            if (!navigator.clipboard) throw new Error("no clipboard api");
                            await navigator.clipboard.writeText(game.phase2Password ?? "");
                            setCopied(true);
                            setTimeout(() => setCopied(false), 2000);
                          } catch {
                            setCopyFailed(true);
                          }
                        }}
                        className="mt-3 font-mono text-[10px] uppercase tracking-[0.2em] text-neon hover:underline"
                      >
                        {copied ? "// copied" : "// copy password"}
                      </button>
                      {copyFailed && (
                        <p className="mt-2 font-mono text-[10px] uppercase tracking-[0.2em] text-magenta">
                          // copy unavailable — select the password above manually
                        </p>
                      )}
                      <p className="mt-3 font-mono text-[10px] text-muted-foreground">
                        Write this down now — it is shown once, on this screen only.
                      </p>
                    </div>
                  )}

                  <a href={ctfdUrl} target="_blank" rel="noreferrer" className="mt-4 inline-block">
                    <DwrButton icon={<ExternalLink className="h-4 w-4" />}>
                      Enter Round 2
                    </DwrButton>
                  </a>
                  <p className="mt-3 break-all font-mono text-[10px] text-muted-foreground">
                    {ctfdUrl}
                  </p>
                </>
              ) : (
                <p className="mt-2 font-mono text-xs text-magenta">
                  Round 2 link is not configured. Ask an event organiser for the URL.
                  {/* Operator hint: set PHASE2_CTFD_URL on the backend, or
                      VITE_CTFD_URL at build time as a fallback. */}
                </p>
              )}
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
