import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  GamePlay,
  GameState,
  JSONFields,
  Login,
  Responses,
  TeamState,
  getApiBaseUrl,
  getWebSocketUrl,
  TOTAL_ROUNDS,
  type ServerMessage,
} from "./dwr-protocol";

export type PromptPhase = "idle" | "sending" | "accepted" | "invalid" | "out_of_chances" | "skipped";

export type RoundRecord = { round: number; score: number };

/** One row of the final ranked leaderboard sent by the backend. */
export type LeaderboardEntry = {
  team_id: number;
  team_name: string;
  score: number;
  rank: number;
};

export type GameSnapshot = {
  /** socket lifecycle */
  connected: boolean;
  connecting: boolean;
  socketError: string | null;

  /** session */
  userId: number | null;
  adminId: number | null;
  /** Opaque per-session secret required by the admin HTTP endpoints. Never
   * derive admin authorization from adminId alone — it's a small, guessable
   * integer and is only kept around for display purposes. */
  adminToken: string | null;
  username: string | null;
  loggingIn: boolean;
  loginError: string | null;

  /** lobby */
  gameState: number;
  teamState: number;
  roster: string[];
  connectedMembers: number;
  countdownEndsAt: number | null;

  /** gameplay */
  image: string | null;
  isMyTurn: boolean;
  turnEndsAt: number | null;
  turnSeconds: number;
  promptPhase: PromptPhase;
  attemptsLeft: number;

  /** scoring */
  currentRound: number;
  /** Authoritative round count from the server. The backend drives its loop
   * from GameServer.total_rounds and now sends it on the wire, so the UI no
   * longer has to hardcode 5 and hope the two agree. */
  totalRounds: number;
  rounds: RoundRecord[];
  lastRoundScore: number | null;
  teamScore: number | null;
  teamScores: number[] | null;
  rank: number | null;
  top3: [number, number][] | null;
  leaderboard: LeaderboardEntry[] | null;
  roundBreakEndsAt: number | null;

  /** phase 2 — null until the server sends GAME_OVER */
  qualified: boolean | null;
  /** CTFd entry point. Only sent to teams that qualified. */
  phase2Url: string | null;
  /** Generated CTFd credentials for this team, only sent if it qualified. */
  phase2Username: string | null;
  phase2Password: string | null;
};

const initialSnapshot: GameSnapshot = {
  connected: false,
  connecting: false,
  socketError: null,

  userId: null,
  adminId: null,
  adminToken: null,
  username: null,
  loggingIn: false,
  loginError: null,

  gameState: GameState.LOGIN_PERIOD,
  teamState: TeamState.WAITING,
  roster: [],
  connectedMembers: 0,
  countdownEndsAt: null,

  image: null,
  isMyTurn: false,
  turnEndsAt: null,
  turnSeconds: 0,
  promptPhase: "idle",
  attemptsLeft: 3,

  currentRound: 0,
  totalRounds: TOTAL_ROUNDS,
  rounds: [],
  lastRoundScore: null,
  teamScore: null,
  teamScores: null,
  rank: null,
  top3: null,
  leaderboard: null,
  roundBreakEndsAt: null,

  qualified: null,
  phase2Url: null,
  phase2Username: null,
  phase2Password: null,
};

type GameContextValue = GameSnapshot & {
  login: (username: string, password: string) => void;
  logout: () => void;
  sendPrompt: (prompt: string) => void;
  skipTurn: () => void;
  resetSession: () => void;
  adminStartGame: () => Promise<{ ok: boolean; message: string }>;
  adminDashboard: () => Promise<AdminDashboard | null>;
};

/** Shape of one team row from /admin/dashboard. Exported for reference;
 * admin.dashboard.tsx intentionally uses a looser local type. */
export type AdminDashboardTeam = {
  team_id: number;
  team_name: string;
  total_members: number;
  assigned_members: number;
  connected_members: number;
  score: number;
  round_scores: number[];
  rank: number | null;
  members: string[];
};

/** Circuit-breaker state for one image provider. */
export type ProviderStatus = {
  provider: string;
  state: "closed" | "open" | "half_open";
  consecutive_failures: number;
  total_successes: number;
  total_failures: number;
  seconds_until_retry: number;
};

export type AdminDashboard = {
  game_state: number;
  total_connected_players: number;
  connected_teams: number;
  total_teams: number;
  /** 1-based. The server sets this to round_num + 1 already — do not add 1. */
  current_round: number;
  total_rounds: number;
  /** "supabase" | "cache" | "hardcoded" — confirms the roster source. */
  roster_source?: string;
  /** Per-provider breaker state, so a failover is visible not inferred. */
  image_providers?: ProviderStatus[];
};

const GameContext = createContext<GameContextValue | null>(null);

/**
 * Credentials for silently re-authenticating after a DROPPED SOCKET.
 *
 * Deliberately a module-level variable and not sessionStorage, and the
 * difference is the whole point:
 *
 *   socket drops (wifi blip, laptop sleeps, server restarts)
 *       -> onclose -> retry -> onopen -> re-login from this variable.
 *          The player never notices. This is worth keeping.
 *
 *   page reload / new tab / browser restart
 *       -> the module is re-evaluated, this is empty, no auto-login.
 *          The player lands on the login screen, which is what a reload
 *          should do.
 *
 * sessionStorage could not tell those two apart. It survives a reload, so a
 * refresh silently signed the player back in -- and because the auto-login
 * path did not go through `login()`, `username` was never put into state, so
 * the UI came back logged in under nobody's name. That is the "ghost" half of
 * the problem, and holding the credentials in memory removes it by
 * construction rather than by patching the symptom.
 *
 * It also means credentials are never written to disk, which is a smaller but
 * real win: sessionStorage is readable by any script on the origin.
 */
type StoredSession = { username: string; password: string; kind: "player" | "admin" };

let liveSession: StoredSession | null = null;

function readStoredSession(): StoredSession | null {
  return liveSession;
}

function clearStoredSession() {
  liveSession = null;
}

export function GameProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<GameSnapshot>(initialSnapshot);
  const socketRef = useRef<WebSocket | null>(null);
  const pendingRef = useRef<string[]>([]);
  const autoLoginRef = useRef(false);

  const patch = useCallback((next: Partial<GameSnapshot>) => {
    setState((prev) => ({ ...prev, ...next }));
  }, []);

  const send = useCallback((payload: Record<string, unknown>) => {
    const raw = JSON.stringify(payload);
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(raw);
    else pendingRef.current.push(raw);
  }, []);

  const handleMessage = useCallback(
    (data: ServerMessage) => {
      const type = data[JSONFields.TYPE];

      // "Is anyone still there?" Answered immediately, before anything else
      // in this function, because the server is holding another player's
      // login request open waiting for it and a late answer is treated as no
      // answer -- which would sign THIS device out of a session it is using.
      //
      // Sent straight down the socket rather than through send(), which
      // queues when the socket is not open. A probe that arrives on a closed
      // socket needs no reply; queueing one would answer the *next* probe
      // instantly and wrongly, vouching for a session that had since died.
      if (type === Responses.SESSION_PROBE) {
        const socket = socketRef.current;
        if (socket && socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ [JSONFields.TYPE]: Login.SESSION_PROBE_ACK }));
        }
        return;
      }


      // total_rounds arrives on the login response, GAME_RUNNING and every
      // ROUND_OVER frame. Capturing it once here keeps the UI in step with
      // GameServer.total_rounds instead of relying on a hardcoded 5.
      const serverRounds = data[JSONFields.TOTAL_ROUNDS];
      if (typeof serverRounds === "number" && serverRounds > 0) {
        patch({ totalRounds: serverRounds });
      }

      // The backend sends these for malformed frames, unauthenticated
      // requests, rate limiting, out-of-turn prompts and image-generation
      // failures. The constant was missing from dwr-protocol.ts, so every
      // one of them fell through to the bottom of this function and was
      // silently discarded — the user just saw nothing happen.
      if (type === Responses.ERROR_RESPONSE) {
        const message = (data[JSONFields.MESSAGE] as string) ?? "The game server rejected that request.";
        console.warn("Server error frame:", message);
        patch({ socketError: message, loggingIn: false });
        return;
      }

      if (type === Responses.ADMIN_RESPONSE) {
        if (data[JSONFields.AUTHORISED] === Login.ACCEPTED) {
          patch({
            adminId: (data[JSONFields.ADMIN_ID] as number) ?? null,
            adminToken: (data[JSONFields.ADMIN_TOKEN] as string) ?? null,
            loggingIn: false,
            loginError: null,
          });
        } else {
          patch({ loggingIn: false, loginError: "Invalid administrator credentials." });
        }
        return;
      }

      if (type === Responses.LOGIN_RESPONSE) {
        if (data[JSONFields.AUTHORISED] === Login.ACCEPTED) {
          const teamState = (data[JSONFields.TEAM_STATE] as number) ?? TeamState.WAITING;
          const gameState = (data[JSONFields.GAME_STATE] as number) ?? GameState.LOGIN_PERIOD;
          const time = (data[JSONFields.TIME] as number) ?? 0;

          const next: Partial<GameSnapshot> = {
            userId: (data[JSONFields.USER_ID] as number) ?? null,
            loggingIn: false,
            loginError: null,
            gameState,
            teamState,
            connectedMembers: (data[JSONFields.CONNECTED_TEAM_MEMBERS] as number) ?? 0,
          };


          if (gameState === GameState.COUNTDOWN) {
            next.countdownEndsAt = Date.now() + time * 1000;
          }

          if (teamState === TeamState.PLAYING) {
            next.image = (data[JSONFields.IMAGE] as string) ?? null;
            next.isMyTurn = Boolean(data[JSONFields.IS_PLAYER_TURN]);
            next.turnSeconds = Math.max(0, Math.round(time));
            next.turnEndsAt = Date.now() + time * 1000;
            next.promptPhase =
              data[JSONFields.PROMPT_STATUS] === GamePlay.PROMPTED ? "accepted" : "idle";
          }

          if (teamState === TeamState.DONE) {
            // TEAM_SCORE is now always a number and the per-round breakdown
            // has its own field, so this no longer has to type-sniff.
            next.teamScores = (data[JSONFields.ROUND_SCORES] as number[]) ?? null;
            next.teamScore = (data[JSONFields.TEAM_SCORE] as number) ?? null;
            next.rank = (data[JSONFields.TEAM_RANK] as number) ?? null;
            next.top3 = (data[JSONFields.TOP3] as [number, number][]) ?? null;
            next.leaderboard = (data[JSONFields.LEADERBOARD] as LeaderboardEntry[]) ?? null;
            next.qualified = (data[JSONFields.QUALIFIED] as boolean) ?? null;
            next.phase2Url = (data[JSONFields.PHASE2_URL] as string) ?? null;
            next.phase2Username = (data[JSONFields.PHASE2_USERNAME] as string) ?? null;
            next.phase2Password = (data[JSONFields.PHASE2_PASSWORD] as string) ?? null;
          }

          patch(next);
        } else if (data[JSONFields.AUTHORISED] === Login.ALREADY_CONNECTED) {
          // The password was correct. Retrying cannot help, and five retries
          // trip the brute-force lockout -- so clear the stored credentials
          // to stop the reconnect loop hammering it, and say what to do.
          clearStoredSession();
          autoLoginRef.current = false;
          patch({
            loggingIn: false,
            loginError:
              (data[JSONFields.MESSAGE] as string) ??
              "This account is already signed in on another device. Log out there first.",
          });
        } else if (data[JSONFields.AUTHORISED] === Login.LOCKED) {
          // Distinct from a wrong password: the account is rate-limited.
          // Telling the user "invalid credentials" here made them retry
          // immediately, which is exactly what extends the lockout.
          const retryAfter = Math.ceil((data[JSONFields.RETRY_AFTER] as number) ?? 60);
          patch({
            loggingIn: false,
            loginError: `Too many failed attempts. Try again in ${retryAfter}s.`,
          });
        } else {
          patch({
            loggingIn: false,
            loginError: "Invalid Team ID or password.",
          });
        }
        return;
      }

      // The server signed this connection out because the same account
      // logged in somewhere else. Clear everything and say why -- the socket
      // is about to be closed by the server, and the reconnect loop must not
      // then log straight back in with the credentials that were just used to
      // evict us, which would have the two devices fighting over the session.
      if (type === Responses.SESSION_REPLACED) {
        const message =
          (data[JSONFields.MESSAGE] as string) ??
          "You were signed out because this account signed in on another device.";
        clearStoredSession();
        autoLoginRef.current = false;
        setState((prev) => ({
          ...initialSnapshot,
          connected: prev.connected,
          loginError: message,
        }));
        return;
      }

      if (type === Responses.LOGOUT_RESPONSE) {
        if (data[JSONFields.AUTHORISED] === Login.ACCEPTED) {
          clearStoredSession();
          setState((prev) => ({ ...initialSnapshot, connected: prev.connected }));
        }
        return;
      }

      if (type === Responses.GAME_STATE_RESPONSE) {
        const gameState = data[JSONFields.GAME_STATE] as number;
        const time = (data[JSONFields.TIME] as number) ?? 0;

        if (gameState === GameState.COUNTDOWN) {
          patch({ gameState, countdownEndsAt: Date.now() + time * 1000 });
        } else if (gameState === GameState.GAME_RUNNING) {
          patch({ gameState, countdownEndsAt: null });
        } else if (gameState === GameState.ROUND_OVER) {
          const round = (data[JSONFields.ROUND] as number) ?? 0;
          const roundScore = (data[JSONFields.ROUND_SCORE] as number) ?? 0;
          setState((prev) => ({
            ...prev,
            gameState,
            currentRound: round,
            lastRoundScore: roundScore,
            teamScore: (data[JSONFields.TEAM_SCORE] as number) ?? prev.teamScore,
            rounds: [...prev.rounds.filter((r) => r.round !== round), { round, score: roundScore }].sort(
              (a, b) => a.round - b.round,
            ),
            roundBreakEndsAt: Date.now() + time * 1000,
            isMyTurn: false,
            turnEndsAt: null,
            promptPhase: "idle",
          }));
        } else if (gameState === GameState.GAME_OVER) {
          patch({
            gameState,
            teamScores: (data[JSONFields.ROUND_SCORES] as number[]) ?? null,
            teamScore: (data[JSONFields.TEAM_SCORE] as number) ?? null,
            rank: (data[JSONFields.TEAM_RANK] as number) ?? null,
            top3: (data[JSONFields.TOP3] as [number, number][]) ?? null,
            // Full ranked table including team names, so the results screen
            // can show a real leaderboard instead of bare team ids.
            leaderboard: (data[JSONFields.LEADERBOARD] as LeaderboardEntry[]) ?? null,
            // Top 50% of the final standings. `phase2Url` is only present on
            // the frames sent to teams that qualified.
            qualified: (data[JSONFields.QUALIFIED] as boolean) ?? null,
            phase2Url: (data[JSONFields.PHASE2_URL] as string) ?? null,
            phase2Username: (data[JSONFields.PHASE2_USERNAME] as string) ?? null,
            phase2Password: (data[JSONFields.PHASE2_PASSWORD] as string) ?? null,
            isMyTurn: false,
            turnEndsAt: null,
            roundBreakEndsAt: null,
          });
        }
        return;
      }

      if (type === Responses.GAMEPLAY_RESPONSE) {
        const message = data[JSONFields.MESSAGE];

        if (message === GamePlay.IMAGE_IN) {
          const time = (data[JSONFields.TIME] as number) ?? 0;
          patch({
            gameState: GameState.GAME_RUNNING,
            teamState: TeamState.PLAYING,
            image: (data[JSONFields.IMAGE] as string) ?? null,
            isMyTurn: Boolean(data[JSONFields.IS_PLAYER_TURN]),
            turnSeconds: Math.max(0, Math.round(time)),
            turnEndsAt: Date.now() + time * 1000,
            promptPhase: "idle",
            attemptsLeft: 3,
            roundBreakEndsAt: null,
          });
        } else if (message === GamePlay.RECEIVED) {
          patch({ promptPhase: "accepted" });
        } else if (message === GamePlay.NOT_RECEIVED) {
          patch({ promptPhase: "skipped" });
        } else if (message === GamePlay.INVALID_PROMPT) {
          patch({
            promptPhase: "invalid",
            attemptsLeft: (data[JSONFields.ATTEMPTS_LEFT] as number) ?? 0,
          });
        } else if (message === GamePlay.OUT_OF_CHANCES) {
          patch({ promptPhase: "out_of_chances", attemptsLeft: 0 });
        }
        return;
      }

      if (type === Responses.TEAM_STATE_RESPONSE) {
        const teamState = data[JSONFields.TEAM_STATE] as number;
        if (teamState === TeamState.JOINED || teamState === TeamState.LEFT) {
          const names = data[JSONFields.USERNAME];
          patch({
            roster: Array.isArray(names) ? (names as string[]) : [],
            connectedMembers: Array.isArray(names) ? names.length : 0,
          });
        } else if (teamState === TeamState.DONE) {
          patch({ teamState: TeamState.DONE, isMyTurn: false, turnEndsAt: null });
        }
      }
    },
    [patch],
  );

  // Single long-lived socket for the whole app.
  useEffect(() => {
    let cancelled = false;
    let retry: ReturnType<typeof setTimeout> | undefined;

    const open = () => {
      if (cancelled) return;
      const url = getWebSocketUrl();
      if (!url) return;

      patch({ connecting: true, socketError: null });
      let socket: WebSocket;
      try {
        socket = new WebSocket(url);
      } catch {
        patch({ connecting: false, socketError: "Unable to reach the game server." });
        retry = setTimeout(open, 3000);
        return;
      }
      socketRef.current = socket;

      socket.onopen = () => {
        patch({ connected: true, connecting: false, socketError: null });
        const queued = pendingRef.current;
        pendingRef.current = [];
        queued.forEach((raw) => socket.send(raw));

        // Re-authenticate after a refresh / reconnect.
        const stored = readStoredSession();
        if (stored && !autoLoginRef.current) {
          autoLoginRef.current = true;
          // `username` has to be patched here too. login() sets it, but this
          // path bypasses login() entirely -- so a reconnect used to restore
          // the session without restoring the name, and the UI showed a
          // logged-in player with no identity.
          patch({ username: stored.username });
          socket.send(
            JSON.stringify({
              [JSONFields.TYPE]: Login.LOGIN,
              [JSONFields.USERNAME]: stored.username,
              [JSONFields.PASSWORD]: stored.password,
            }),
          );
        }
      };

      socket.onmessage = (event) => {
        try {
          handleMessage(JSON.parse(event.data as string) as ServerMessage);
        } catch (err) {
          console.error("Malformed packet from game server", err);
        }
      };

      socket.onerror = () => {
        patch({ socketError: "Connection to the game server failed." });
      };

      socket.onclose = () => {
        socketRef.current = null;
        autoLoginRef.current = false;
        patch({ connected: false, connecting: false });
        if (!cancelled) retry = setTimeout(open, 2000);
      };
    };

    open();

    return () => {
      cancelled = true;
      if (retry) clearTimeout(retry);
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket) {
        socket.onclose = null;
        socket.close();
      }
    };
  }, [handleMessage, patch]);

  const login = useCallback(
    (username: string, password: string) => {
      patch({ loggingIn: true, loginError: null, username });
      liveSession = { username, password, kind: "player" };
      autoLoginRef.current = true;
      send({
        [JSONFields.TYPE]: Login.LOGIN,
        [JSONFields.USERNAME]: username,
        [JSONFields.PASSWORD]: password,
      });
    },
    [patch, send],
  );

  const logout = useCallback(() => {
    clearStoredSession();
    autoLoginRef.current = false;
    send({ [JSONFields.TYPE]: Login.LOGOUT });
  }, [send]);

  const resetSession = useCallback(() => {
    clearStoredSession();
    autoLoginRef.current = false;
    setState((prev) => ({ ...initialSnapshot, connected: prev.connected }));
  }, []);

  const sendPrompt = useCallback(
    (prompt: string) => {
      patch({ promptPhase: "sending" });
      send({
        [JSONFields.TYPE]: GamePlay.PROMPT_OUT,
        [JSONFields.STATUS]: GamePlay.PROMPTED,
        [JSONFields.PROMPT]: prompt,
      });
    },
    [patch, send],
  );

  const skipTurn = useCallback(() => {
    send({
      [JSONFields.TYPE]: GamePlay.PROMPT_OUT,
      [JSONFields.STATUS]: GamePlay.NOT_PROMPTED,
      [JSONFields.PROMPT]: "",
    });
  }, [send]);

  const adminId = state.adminId;
  const adminToken = state.adminToken;

  // Admin HTTP endpoints authenticate via the opaque X-Admin-Token issued at
  // login, not the admin id. The id is a small guessable integer (0, 1, ...);
  // trusting it directly would let anyone set that header and hit
  // /admin/rungame without ever knowing an admin password, as long as some
  // admin happened to be connected.
  const adminStartGame = useCallback(async () => {
    if (adminId === null || !adminToken) return { ok: false, message: "Admin session not authenticated." };
    try {
      const res = await fetch(`${getApiBaseUrl()}/admin/rungame`, {
        method: "POST",
        headers: { "X-Admin-Token": adminToken },
      });
      const body = (await res.json().catch(() => ({}))) as { detail?: string; message?: string };
      if (!res.ok) {
        return { ok: false, message: body.detail ?? `Request failed (${res.status})` };
      }
      return { ok: true, message: body.message ?? "Game countdown initiated." };
    } catch {
      return { ok: false, message: "Unable to reach the game server." };
    }
  }, [adminId, adminToken]);

  const adminDashboard = useCallback(async () => {
    if (adminId === null || !adminToken) return null;
    try {
      const res = await fetch(`${getApiBaseUrl()}/admin/dashboard`, {
        headers: { "X-Admin-Token": adminToken },
      });
      if (!res.ok) return null;
      return (await res.json()) as AdminDashboard;
    } catch {
      return null;
    }
  }, [adminId, adminToken]);

  const value = useMemo<GameContextValue>(
    () => ({
      ...state,
      login,
      logout,
      sendPrompt,
      skipTurn,
      resetSession,
      adminStartGame,
      adminDashboard,
    }),
    [state, login, logout, sendPrompt, skipTurn, resetSession, adminStartGame, adminDashboard],
  );

  return <GameContext.Provider value={value}>{children}</GameContext.Provider>;
}

export function useGame(): GameContextValue {
  const ctx = useContext(GameContext);
  if (!ctx) throw new Error("useGame must be used inside <GameProvider>");
  return ctx;
}

/** Seconds remaining until `endsAt`, ticking once per second. */
export function useSecondsUntil(endsAt: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (endsAt === null) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [endsAt]);
  if (endsAt === null) return 0;
  return Math.max(0, Math.ceil((endsAt - now) / 1000));
}
