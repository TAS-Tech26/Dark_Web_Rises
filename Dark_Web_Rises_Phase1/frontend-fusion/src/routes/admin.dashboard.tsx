import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import { TopNav } from "@/components/dwr/TopNav";
import { Panel } from "@/components/dwr/Panel";
import { DwrButton } from "@/components/dwr/DwrButton";
import { StatusPill } from "@/components/dwr/StatusPill";
import { useGame, type AdminDashboard as BaseAdminDashboardStats } from "@/lib/game-connection";
import { GameState } from "@/lib/dwr-protocol";
import { Play, Radio, Loader2, Users, Trophy } from "lucide-react";

export const Route = createFileRoute("/admin/dashboard")({
  component: AdminDashboard,
});

type TeamMember = {
  id?: string | number;
  name?: string;
  is_connected?: boolean;
};

type TeamData = {
  team_id?: string | number;
  id?: string | number;
  team_name: string;
  members?: TeamMember[] | string[] | number[];
  connected_members?: number; // Count of online members provided by backend
  total_members?: number;     // Total assigned members
  score?: number;             // Team score for leaderboard
};

type AdminDashboardStats = BaseAdminDashboardStats & {
  team_names?: TeamData[];
};

type Section = "overview" | "teams" | "leaderboard" | "rounds";

const GAME_STATE_LABEL: Record<number, string> = {
  [GameState.LOGIN_PERIOD]: "Login Period",
  [GameState.PREGAME]: "Pre-Game",
  [GameState.GAME_RUNNING]: "Running",
  [GameState.COUNTDOWN]: "Countdown",
  [GameState.GAME_OVER]: "Game Over",
  [GameState.ROUND_OVER]: "Round Over",
};

function AdminDashboard() {
  const navigate = useNavigate();
  const game = useGame();
  const [section, setSection] = useState<Section>("overview");
  const [stats, setStats] = useState<AdminDashboardStats | null>(null);
  const [starting, setStarting] = useState(false);
  const [aborting, setAborting] = useState(false);
  const [confirmAbort, setConfirmAbort] = useState(false);
  const [notice, setNotice] = useState<{ ok: boolean; message: string } | null>(null);

  const { adminId, adminDashboard, adminStartGame, adminAbortGame, logout } = game;

  const handleLogout = () => {
    logout();
    navigate({ to: "/admin/login" });
  };

  useEffect(() => {
    const handleBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, []);

  useEffect(() => {
    if (adminId === null && game.connected && !game.loggingIn) {
      navigate({ to: "/admin/login" });
    }
  }, [adminId, game.connected, game.loggingIn, navigate]);

  useEffect(() => {
    if (adminId === null) return;
    let active = true;
    const tick = async () => {
      const next = await adminDashboard();
      if (active && next) setStats(next as AdminDashboardStats);
    };
    void tick();
    const timer = setInterval(() => void tick(), 3000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [adminId, adminDashboard]);

  const startGame = useCallback(async () => {
    setStarting(true);
    setNotice(null);
    const result = await adminStartGame();
    setStarting(false);
    setNotice(result);
  }, [adminStartGame]);

  // Abort: end the event now, keeping the scores collected so far.
  //
  // The escape hatch for a game that is stuck. Once game_state leaves
  // LOGIN_PERIOD, /admin/rungame refuses everything ("already started"), so a
  // hung round or a dead driver task previously left the only recovery as a
  // process restart -- which then needs all 700 players to log in again.
  //
  // Deliberately behind an explicit confirm step rather than a browser
  // confirm() dialog: this ends the event for 700 people, and a modal dialog
  // blocking the moderator's tab is its own hazard.
  const abortGame = useCallback(async () => {
    setAborting(true);
    setNotice(null);
    const result = await adminAbortGame();
    setAborting(false);
    setConfirmAbort(false);
    setNotice(result);
  }, [adminAbortGame]);

  const nav: { key: Section; label: string }[] = [
    { key: "overview", label: "Overview" },
    { key: "teams", label: "Teams" },
    { key: "leaderboard", label: "Leaderboard" },
    { key: "rounds", label: "Rounds" },
  ];

  return (
    <div className="min-h-screen">
      <TopNav status="live" />
      <main className="relative mx-auto max-w-[1400px] px-6 py-8">
        <div className="pointer-events-none fixed inset-0 bg-grid opacity-20" />

        <div className="relative">
          <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
            <div>
              <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
                // ops console
              </div>
              <h1 className="font-mono text-3xl font-black uppercase tracking-tight text-foreground sm:text-4xl">
                Admin <span className="text-magenta">Dashboard</span>
              </h1>
            </div>
            <div className="flex items-center gap-4">
              <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                <Radio
                  className={`h-3 w-3 ${game.connected ? "animate-pulse text-neon" : "text-destructive"}`}
                />
                {game.connected ? "broadcasting" : "offline"} ·{" "}
                {stats?.connected_teams ?? 0}/{stats?.total_teams ?? 0} teams connected
              </div>

              <DwrButton variant="danger" size="sm" onClick={handleLogout}>
                Logout
              </DwrButton>
            </div>
          </div>

          {/* Tab navigation strip */}
          <div className="mb-6 flex flex-wrap gap-1 border-b border-border">
            {nav.map((n) => (
              <button
                key={n.key}
                onClick={() => setSection(n.key)}
                className={`relative px-4 py-2 font-mono text-xs uppercase tracking-widest transition-colors ${
                  section === n.key
                    ? "text-neon"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                {n.label}
                {section === n.key && (
                  <span className="absolute bottom-[-1px] left-0 right-0 h-px bg-neon shadow-[0_0_12px_var(--neon)]" />
                )}
              </button>
            ))}
          </div>

          {section === "overview" && (
            <OverviewSection
              stats={stats}
              starting={starting}
              notice={notice}
              onStart={startGame}
              onAbort={abortGame}
              aborting={aborting}
              confirmAbort={confirmAbort}
              setConfirmAbort={setConfirmAbort}
            />
          )}
          {section === "teams" && <TeamsSection stats={stats} />}
          {section === "leaderboard" && <LeaderboardSection stats={stats} />}
          {section === "rounds" && (
            <RoundsSection
              stats={stats}
              starting={starting}
              notice={notice}
              onStart={startGame}
              onAbort={abortGame}
              aborting={aborting}
              confirmAbort={confirmAbort}
              setConfirmAbort={setConfirmAbort}
            />
          )}
        </div>
      </main>
    </div>
  );
}

/* --------------------------------- Sections -------------------------------- */

type ControlProps = {
  stats: AdminDashboardStats | null;
  starting: boolean;
  notice: { ok: boolean; message: string } | null;
  onStart: () => void;
  onAbort: () => void;
  aborting: boolean;
  confirmAbort: boolean;
  setConfirmAbort: (v: boolean) => void;
};

function OverviewSection({ stats, starting, notice, onStart, onAbort, aborting, confirmAbort, setConfirmAbort }: ControlProps) {
  const cards = [
    { label: "Total Teams", value: String(stats?.total_teams ?? "—") },
    {
      label: "Teams Connected",
      value: String(stats?.connected_teams ?? "—"),
      accent: "text-neon",
    },
    {
      label: "Players Online",
      value: String(stats?.total_connected_players ?? "—"),
      accent: "text-foreground",
    },
    {
      label: "Round",
      value: stats
        ? `${String(Math.min(stats.current_round, stats.total_rounds)).padStart(2, "0")} / ${stats.total_rounds}`
        : "—",
      accent: "text-magenta",
    },
  ];

  return (
    <div className="space-y-6">
      <Panel className="flex flex-wrap items-center justify-between gap-4 p-5">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
            Game State
          </div>
          <div className="mt-1 font-mono text-2xl font-bold text-neon">
            {stats ? (GAME_STATE_LABEL[stats.game_state] ?? "Unknown") : "—"}
          </div>
        </div>
        <div className="flex items-center gap-3">
          {notice && (
            <span
              className={`font-mono text-xs ${notice.ok ? "text-neon" : "text-destructive"}`}
            >
              {notice.message}
            </span>
          )}
          <DwrButton
            disabled={starting}
            onClick={onStart}
            icon={
              starting ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Play className="h-3 w-3" />
              )
            }
          >
            {starting ? "Starting..." : "Run Game"}
          </DwrButton>
        </div>
      </Panel>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {cards.map((s) => (
          <Panel key={s.label} className="p-5">
            <div className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
              {s.label}
            </div>
            <div className={`mt-2 font-mono text-3xl font-bold tabular-nums ${s.accent ?? "text-foreground"}`}>
              {s.value}
            </div>
          </Panel>
        ))}
      </div>
      <div className="grid gap-6 lg:grid-cols-[2fr_1fr]">
        <TeamsSection compact stats={stats} />
        <div className="space-y-4">
          <RoundsSection
            compact
            stats={stats}
            starting={starting}
            notice={notice}
            onStart={onStart}
            onAbort={onAbort}
            aborting={aborting}
            confirmAbort={confirmAbort}
            setConfirmAbort={setConfirmAbort}
          />
        </div>
      </div>
    </div>
  );
}

function TeamsSection({
  compact,
  stats,
}: {
  compact?: boolean;
  stats: AdminDashboardStats | null;
}) {
  const teamsList = stats?.team_names ?? [];

  return (
    <Panel className="overflow-hidden">
      <div className="flex items-center justify-between border-b border-border px-5 py-3">
        <h3 className="flex items-center gap-2 font-mono text-sm uppercase tracking-[0.25em] text-neon">
          <Users className="h-4 w-4" />
          {compact ? "Live Teams" : "Teams Directory"}
        </h3>
        <StatusPill status={stats ? "live" : "waiting"} />
      </div>

      <div className="max-h-[380px] overflow-y-auto divide-y divide-border font-mono">
        {teamsList.length === 0 ? (
          <div className="px-5 py-8 text-center text-xs uppercase tracking-widest text-muted-foreground">
            No team data available from backend
          </div>
        ) : (
          teamsList.map((team, idx) => {
            const teamId = team.team_id ?? team.id ?? idx + 1;
            
            // Total assigned members
            const totalAssigned = team.total_members ?? (Array.isArray(team.members) ? team.members.length : 0);
            
            // Connected members count
            let connectedCount = team.connected_members;
            if (connectedCount === undefined && Array.isArray(team.members)) {
              connectedCount = team.members.filter(
                (m) => typeof m === "object" && m !== null && m.is_connected
              ).length;
            }
            if (connectedCount === undefined) {
              connectedCount = 0;
            }

            return (
              <div
                key={String(teamId)}
                className="flex items-center justify-between px-5 py-3 transition-colors hover:bg-surface/50"
              >
                <div className="flex items-center gap-3">
                  <span className="rounded border border-border bg-surface px-2 py-0.5 text-[10px] text-muted-foreground">
                    ID: {teamId}
                  </span>
                  <span className="text-xs font-bold uppercase tracking-wider text-foreground">
                    {team.team_name}
                  </span>
                </div>

                {/* Display connected players out of assigned total */}
                <div className="flex items-center gap-2 text-xs">
                  <span
                    className={`inline-block h-2 w-2 rounded-full ${
                      connectedCount > 0 ? "bg-neon shadow-[0_0_8px_var(--neon)]" : "bg-muted-foreground/30"
                    }`}
                  />
                  <span className="font-bold tabular-nums text-foreground">
                    {connectedCount} / {totalAssigned}
                  </span>
                  <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
                    online
                  </span>
                </div>
              </div>
            );
          })
        )}
      </div>

      <div className="border-t border-border px-5 py-3 font-mono text-[11px] text-muted-foreground">
        {"> "} Showing {teamsList.length} registered team(s) synced from server.
      </div>
    </Panel>
  );
}

/* Dedicated Leaderboard Section */
function LeaderboardSection({ stats }: { stats: AdminDashboardStats | null }) {
  // Sort teams by score descending
  const sortedTeams = [...(stats?.team_names ?? [])].sort(
    (a, b) => (b.score ?? 0) - (a.score ?? 0)
  );

  return (
    <Panel className="overflow-hidden">
      <div className="flex items-center justify-between border-b border-border px-5 py-3">
        <h3 className="flex items-center gap-2 font-mono text-sm uppercase tracking-[0.25em] text-magenta">
          <Trophy className="h-4 w-4" />
          Live Leaderboard
        </h3>
        <StatusPill status={stats ? "live" : "waiting"} />
      </div>

      <div className="divide-y divide-border font-mono">
        {sortedTeams.length === 0 ? (
          <div className="px-5 py-8 text-center text-xs uppercase tracking-widest text-muted-foreground">
            No active standings recorded
          </div>
        ) : (
          sortedTeams.map((team, index) => {
            const rank = index + 1;
            return (
              <div
                key={team.team_id ?? index}
                className="flex items-center justify-between px-5 py-3.5 transition-colors hover:bg-surface/50"
              >
                <div className="flex items-center gap-4">
                  <span
                    className={`flex h-6 w-6 items-center justify-center rounded font-bold text-xs ${
                      rank === 1
                        ? "bg-amber-500/20 text-amber-400 border border-amber-500/40"
                        : rank === 2
                        ? "bg-slate-300/20 text-slate-300 border border-slate-300/40"
                        : rank === 3
                        ? "bg-amber-700/20 text-amber-600 border border-amber-700/40"
                        : "bg-surface text-muted-foreground"
                    }`}
                  >
                    #{rank}
                  </span>
                  <span className="text-sm font-bold uppercase tracking-wider text-foreground">
                    {team.team_name}
                  </span>
                </div>

                <div className="flex items-center gap-2">
                  <span className="text-lg font-bold tabular-nums text-magenta">
                    {team.score ?? 0}
                  </span>
                  <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
                    pts
                  </span>
                </div>
              </div>
            );
          })
        )}
      </div>

      <div className="border-t border-border px-5 py-3 font-mono text-[11px] text-muted-foreground">
        {"> "} Leaderboard rankings update dynamically after each round submission.
      </div>
    </Panel>
  );
}

function RoundsSection({
  compact,
  stats,
  starting,
  notice,
  onStart,
  onAbort,
  aborting,
  confirmAbort,
  setConfirmAbort,
}: ControlProps & { compact?: boolean }) {
  const state = stats?.game_state;
  const running = state === GameState.GAME_RUNNING || state === GameState.COUNTDOWN;
  const finished = state === GameState.GAME_OVER;

  return (
    <div className={`grid gap-4 ${compact ? "" : "lg:grid-cols-2"}`}>
      <Panel className="p-5">
        <div className="mb-1 font-mono text-[10px] uppercase tracking-[0.3em] text-muted-foreground">
          Round 01 · Lost In Translation
        </div>
        <div className="mb-4 flex items-center justify-between">
          <div className="font-mono text-xl font-bold text-foreground">
            {stats ? (GAME_STATE_LABEL[stats.game_state] ?? "Unknown") : "—"}
          </div>
          <StatusPill status={finished ? "ended" : running ? "live" : "waiting"} />
        </div>

        <div className="mb-4 space-y-2 font-mono text-xs text-muted-foreground">
          <div className="flex justify-between">
            <span>Current Round</span>
            <span className="text-foreground">
              {stats
                ? `${Math.min(stats.current_round, stats.total_rounds)} / ${stats.total_rounds}`
                : "— / —"}
            </span>
          </div>
          <div className="flex justify-between">
            <span>Players Online</span>
            <span className="text-foreground">{stats?.total_connected_players ?? "—"}</span>
          </div>
        </div>

        <DwrButton
          size="sm"
          className="w-full"
          disabled={starting || running}
          onClick={onStart}
          icon={
            starting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Play className="h-3 w-3" />
          }
        >
          {running ? "Game In Progress" : starting ? "Starting..." : "Run Game"}
        </DwrButton>

        {/* Only offered while a game is actually in flight. Two-step, because
            it ends the event for everyone. */}
        {running && !confirmAbort && (
          <button
            type="button"
            onClick={() => setConfirmAbort(true)}
            className="mt-2 w-full rounded border border-destructive/40 py-2 font-mono text-[10px] uppercase tracking-[0.2em] text-destructive hover:bg-destructive/10"
          >
            End Event Early
          </button>
        )}

        {running && confirmAbort && (
          <div className="mt-2 rounded border border-destructive/50 bg-destructive/10 p-3">
            <div className="font-mono text-[10px] uppercase tracking-[0.2em] text-destructive">
              // end the event now?
            </div>
            <p className="mt-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
              Scores collected so far are kept and broadcast to every connected
              team, and the Phase 2 CSV is written. This cannot be undone.
            </p>
            <div className="mt-3 flex gap-2">
              <button
                type="button"
                disabled={aborting}
                onClick={onAbort}
                className="flex-1 rounded border border-destructive bg-destructive/20 py-2 font-mono text-[10px] uppercase tracking-[0.2em] text-destructive disabled:opacity-50"
              >
                {aborting ? "Ending..." : "Yes, End It"}
              </button>
              <button
                type="button"
                disabled={aborting}
                onClick={() => setConfirmAbort(false)}
                className="flex-1 rounded border border-border py-2 font-mono text-[10px] uppercase tracking-[0.2em] text-muted-foreground disabled:opacity-50"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {notice && (
          <div
            className={`mt-3 rounded border p-2 text-center font-mono text-[11px] ${
              notice.ok
                ? "border-neon/40 bg-neon/10 text-neon"
                : "border-destructive/40 bg-destructive/10 text-destructive"
            }`}
          >
            {notice.message}
          </div>
        )}

        <div className="mt-4 rounded border border-border bg-surface/60 p-3 font-mono text-[11px] text-muted-foreground">
          {"> "}Round progression, timers and scoring are fully driven by the backend.
        </div>
      </Panel>
    </div>
  );
}