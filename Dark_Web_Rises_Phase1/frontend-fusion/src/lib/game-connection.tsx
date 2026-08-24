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
  /** Clear the current transient error banner. */
  dismissError: () => void;
  adminStartGame: () => Promise<{ ok: boolean; message: string }>;
  /** End the event now, keeping scores so far. Backed by POST /admin/abort. */
  adminAbortGame: () => Promise<{ ok: boolean; message: string }>;
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
 * That distinction was the original reason this was memory-only, and the
 * reasoning holds for a LAPTOP, where a reload is a deliberate act. It does not
 * hold for 700 phones. iOS Safari and Chrome on Android routinely DISCARD a
 * backgrounded tab under memory pressure and re-run the document when the user
 * returns -- the player did not reload, they answered a text message. Memory-
 * only meant they came back to an empty login form, retyping a team password
 * mid-round, then waiting out the server's 3s session probe. "Please do not
 * refresh" is not a control we have over an OS tab discard.
 *
 * The "ghost login" that motivated the memory-only choice is separately fixed:
 * the reconnect path in onopen now patches `username` into state before sending
 * the frame, exactly as login() does. Persistence was the heavier remedy for a
 * problem that only needed that one line.
 *
 * sessionStorage rather than localStorage: scoped to the tab, cleared when the
 * tab closes, not shared with other tabs. Every access is wrapped, because it
 * throws in private mode and does not exist during SSR.
 */
type StoredSession = { username: string; password: string; kind: "player" | "admin" };

const SESSION_KEY = "dwr.session";

// In-memory cache in front of sessionStorage, so the common path never touches
// storage and an environment that forbids it still works for the socket-drop
// case.
let liveSession: StoredSession | null = null;

function readStoredSession(): StoredSession | null {
  if (liveSession) return liveSession;
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(SESSION_KEY);
    if (raw) liveSession = JSON.parse(raw) as StoredSession;
  } catch {
    // Private mode, disabled storage, SSR. Degrade to memory-only.
  }
  return liveSession;
}

function writeStoredSession(session: StoredSession) {
  liveSession = session;
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
  } catch {
    /* memory-only is still correct, just less durable */
  }
}

function clearStoredSession() {
  liveSession = null;
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(SESSION_KEY);
  } catch {
    /* nothing to do */
  }
}

export function GameProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<GameSnapshot>(initialSnapshot);
  const socketRef = useRef<WebSocket | null>(null);
  const autoLoginRef = useRef(false);
  // Consecutive failed connection attempts, for the reconnect backoff.
  const attemptRef = useRef(0);
  // When the tab was last hidden, so a wake can decide whether to distrust the
  // connection it woke up holding.
  const hiddenSinceRef = useRef<number | null>(null);

  const patch = useCallback((next: Partial<GameSnapshot>) => {
    setState((prev) => ({ ...prev, ...next }));
  }, []);

  /**
   * Send if the socket is open. Returns whether it went out.
   *
   * Nothing is queued any more, and that is deliberate. The old pending queue
   * was flushed in onopen BEFORE the re-login frame, so every gameplay frame
   * held across a wifi blip arrived while the server still had
   * current_client_type = None and was answered with "Not authenticated" --
   * destroying the player's prompt on a path that looked, from their side,
   * exactly like a slow server. The queue also had no age limit and was never
   * cleared on logout, so a prompt queued in round 2 could replay on a later
   * socket.
   *
   * Every frame this app sends is either time-bound (a prompt, a skip) or
   * reconstructible (the login, which onopen re-sends from the stored session).
   * There is nothing worth replaying blind.
   */
  const send = useCallback((payload: Record<string, unknown>): boolean => {
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(payload));
      return true;
    }
    return false;
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
        // promptPhase MUST be released here. round-1 unmounts the prompt editor
        // and shows a "transmitting prompt_" spinner while the phase is
        // "sending", so leaving it set turned every rejected prompt -- not your
        // turn, rate limited, generation failed -- into a spinner that span for
        // the rest of the event with no message and no way back.
        setState((prev) => ({
          ...prev,
          socketError: message,
          loggingIn: false,
          promptPhase: prev.promptPhase === "sending" ? "idle" : prev.promptPhase,
        }));
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
            // The server sends the round on every login response; this handler
            // never read it, so currentRound stayed 0 until the next ROUND_OVER
            // frame. A player who reconnected during round 4 was shown
            // "ROUND 01 / 5", and the same stale value drives isFinalRound on
            // the break screen -- so they could be told the event continues
            // when that was the last round.
            currentRound: (data[JSONFields.ROUND] as number) ?? 0,
            // Clear any error left over from the disconnect that got us here.
            socketError: null,
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
            // Also sent by the server for the active player, and also ignored
            // until now: someone who reconnected after two invalid prompts was
            // shown a full budget of 3 attempts remaining.
            next.attemptsLeft = (data[JSONFields.ATTEMPTS_LEFT] as number) ?? 3;
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

    // Exponential backoff with FULL jitter.
    //
    // A fixed 2s retry meant an AP reboot dropped all 700 sockets at the same
    // instant and they reconnected on the same 2-second beat, in phase, for as
    // long as the problem lasted. Each reconnect carries a login frame, and the
    // server verifies it with a ~56ms PBKDF2 -- 700 of those is ~39s of CPU per
    // wave, arriving every 2s. Waves stack and never drain. The per-socket
    // rate limit does not help, because each reconnect is a NEW socket.
    //
    // Full jitter (a uniform pick from [0, base]) rather than a fixed delay
    // plus noise: it spreads a synchronised herd across the whole window
    // instead of preserving its shape.
    const scheduleRetry = () => {
      if (cancelled) return;
      const attempt = Math.min(attemptRef.current++, 6);
      const base = Math.min(30000, 500 * 2 ** attempt);
      retry = setTimeout(open, Math.random() * base);
    };

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
        scheduleRetry();
        return;
      }
      socketRef.current = socket;

      socket.onopen = () => {
        attemptRef.current = 0;
        patch({ connected: true, connecting: false, socketError: null });

        // Re-authenticate after a reconnect. Nothing else is sent before this
        // -- see the note on send() about the old pending queue arriving ahead
        // of the login and being rejected as unauthenticated.
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
        scheduleRetry();
      };
    };

    // Wake handling. This is the dominant failure mode on a phone.
    //
    // When the screen locks or the tab is backgrounded, the OS suspends the
    // page and the TCP connection is frequently NOT torn down cleanly, so
    // `onclose` may not fire for minutes -- or at all. The server has already
    // given up long before that (three failed sends at 1.5s), so the player
    // unlocks their phone to a page that still reads "LIVE", a timer still
    // ticking, and no frames arriving. They miss the turn and their team is
    // penalised.
    //
    // A true application heartbeat needs a ping message the server understands;
    // there isn't one, and inventing a frame here would just earn an
    // ERROR_RESPONSE. What we can do without touching the protocol is distrust
    // the connection we woke up holding: if the tab was hidden long enough for
    // the server to have timed us out, force a reconnect rather than assuming
    // the socket survived. A reconnect is cheap and re-authenticates itself; a
    // zombie socket costs the player their turn.
    const STALE_AFTER_HIDDEN_MS = 20000;

    const reconnectNow = () => {
      if (cancelled) return;
      if (retry) clearTimeout(retry);
      attemptRef.current = 0; // a wake is not congestion; do not back off
      const socket = socketRef.current;
      if (socket && socket.readyState === WebSocket.OPEN) {
        // Looks alive, but we cannot tell from here. Closing routes us through
        // the normal reconnect path, which re-logs in from the stored session.
        socket.close();
        return; // onclose -> scheduleRetry -> open
      }
      socketRef.current = null;
      open();
    };

    const onVisibility = () => {
      if (typeof document === "undefined") return;
      if (document.visibilityState === "hidden") {
        hiddenSinceRef.current = Date.now();
        return;
      }
      const hiddenFor = hiddenSinceRef.current === null ? 0 : Date.now() - hiddenSinceRef.current;
      hiddenSinceRef.current = null;
      const socket = socketRef.current;
      const open_ = socket && socket.readyState === WebSocket.OPEN;
      if (!open_ || hiddenFor > STALE_AFTER_HIDDEN_MS) reconnectNow();
    };

    const onOnline = () => reconnectNow();

    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisibility);
    }
    if (typeof window !== "undefined") {
      window.addEventListener("online", onOnline);
      window.addEventListener("pageshow", onVisibility);
    }

    // Spread the very first connection too. 700 people tapping the link when
    // the moderator says "go" is the same herd, before any socket has dropped.
    retry = setTimeout(open, Math.random() * 2000);

    return () => {
      cancelled = true;
      if (retry) clearTimeout(retry);
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisibility);
      }
      if (typeof window !== "undefined") {
        window.removeEventListener("online", onOnline);
        window.removeEventListener("pageshow", onVisibility);
      }
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
      writeStoredSession({ username, password, kind: "player" });
      autoLoginRef.current = true;
      // If the socket is not open the frame is dropped rather than queued --
      // onopen re-sends it from the session we just stored, which is the same
      // frame and correctly ordered.
      if (!send({
        [JSONFields.TYPE]: Login.LOGIN,
        [JSONFields.USERNAME]: username,
        [JSONFields.PASSWORD]: password,
      })) {
        autoLoginRef.current = false;
      }
    },
    [patch, send],
  );

  const logout = useCallback(() => {
    clearStoredSession();
    autoLoginRef.current = false;
    send({ [JSONFields.TYPE]: Login.LOGOUT });
  }, [send]);

  const dismissError = useCallback(() => patch({ socketError: null }), [patch]);

  const resetSession = useCallback(() => {
    clearStoredSession();
    autoLoginRef.current = false;
    setState((prev) => ({ ...initialSnapshot, connected: prev.connected }));
  }, []);

  const sendPrompt = useCallback(
    (prompt: string) => {
      // Phase is set only AFTER the frame actually leaves. Setting it first
      // locked the editor even when the socket was down, so an offline player
      // was shown a spinner for a prompt that was never sent.
      const sent = send({
        [JSONFields.TYPE]: GamePlay.PROMPT_OUT,
        [JSONFields.STATUS]: GamePlay.PROMPTED,
        [JSONFields.PROMPT]: prompt,
      });
      if (!sent) {
        patch({
          promptPhase: "idle",
          socketError: "You are offline. Reconnecting -- try again in a moment.",
        });
        return;
      }
      patch({ promptPhase: "sending" });
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
  // A 401/403 means this admin session is gone server-side -- the token was
  // revoked, or a second dashboard tab was opened and closed, which empties
  // connected_admins and invalidates the surviving tab's token. Without
  // clearing local state the UI stayed on a dashboard that silently 403'd every
  // poll while still rendering the last numbers it had, and even Log Out did
  // not escape it. Dropping the credentials sends the moderator back to the
  // login screen, which is a page they know how to use.
  const clearDeadAdminSession = useCallback(() => {
    clearStoredSession();
    patch({ adminId: null, adminToken: null });
  }, [patch]);

  const adminStartGame = useCallback(async () => {
    if (adminId === null || !adminToken) return { ok: false, message: "Admin session not authenticated." };
    try {
      const res = await fetch(`${getApiBaseUrl()}/admin/rungame`, {
        method: "POST",
        headers: { "X-Admin-Token": adminToken },
      });
      const body = (await res.json().catch(() => ({}))) as { detail?: string; message?: string };
      if (res.status === 401 || res.status === 403) {
        clearDeadAdminSession();
        return { ok: false, message: "Admin session expired. Please log in again." };
      }
      if (!res.ok) {
        return { ok: false, message: body.detail ?? `Request failed (${res.status})` };
      }
      return { ok: true, message: body.message ?? "Game countdown initiated." };
    } catch {
      return { ok: false, message: "Unable to reach the game server." };
    }
  }, [adminId, adminToken, clearDeadAdminSession]);

  /** End the event now, keeping the scores collected so far. */
  const adminAbortGame = useCallback(async () => {
    if (adminId === null || !adminToken) return { ok: false, message: "Admin session not authenticated." };
    try {
      const res = await fetch(`${getApiBaseUrl()}/admin/abort`, {
        method: "POST",
        headers: { "X-Admin-Token": adminToken },
      });
      const body = (await res.json().catch(() => ({}))) as { detail?: string; message?: string };
      if (res.status === 401 || res.status === 403) {
        clearDeadAdminSession();
        return { ok: false, message: "Admin session expired. Please log in again." };
      }
      if (!res.ok) {
        return { ok: false, message: body.detail ?? `Request failed (${res.status})` };
      }
      return { ok: true, message: body.message ?? "Game aborted; final scores sent." };
    } catch {
      return { ok: false, message: "Unable to reach the game server." };
    }
  }, [adminId, adminToken, clearDeadAdminSession]);

  const adminDashboard = useCallback(async () => {
    if (adminId === null || !adminToken) return null;
    try {
      const res = await fetch(`${getApiBaseUrl()}/admin/dashboard`, {
        headers: { "X-Admin-Token": adminToken },
      });
      if (res.status === 401 || res.status === 403) {
        clearDeadAdminSession();
        return null;
      }
      if (!res.ok) return null;
      return (await res.json()) as AdminDashboard;
    } catch {
      return null;
    }
  }, [adminId, adminToken, clearDeadAdminSession]);

  const value = useMemo<GameContextValue>(
    () => ({
      ...state,
      login,
      logout,
      sendPrompt,
      skipTurn,
      resetSession,
      dismissError,
      adminStartGame,
      adminAbortGame,
      adminDashboard,
    }),
    [
      state, login, logout, sendPrompt, skipTurn, resetSession, dismissError,
      adminStartGame, adminAbortGame, adminDashboard,
    ],
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
