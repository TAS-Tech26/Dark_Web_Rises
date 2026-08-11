/**
 * Wire protocol mirrored 1:1 from the FastAPI backend (`enumerations.py`).
 * DO NOT change these values — the backend is the source of truth.
 */

export const JSONFields = {
  TYPE: "type",
  STATUS: "status",
  USERNAME: "username",
  PASSWORD: "password",
  AUTHORISED: "authorised",
  TEAM_NAME: "team_name",
  USER_ID: "user_id",
  TEAM_ID: "team_id",
  CONNECTED_TEAM_MEMBERS: "connected_team_members",
  GAME_STATE: "game_state",
  TEAM_STATE: "team_state",
  IMAGE: "image",
  MESSAGE: "message",
  PROMPT: "prompt",
  TIME: "time",
  IS_PLAYER_TURN: "is_player_turn",
  PROMPT_STATUS: "prompt_status",
  ROUND: "round",
  ROUND_SCORE: "round_score",
  TEAM_SCORE: "team_score",
  TEAM_RANK: "team_rank",
  TOP3: "top3",
  ADMIN: "admin",
  ADMIN_ID: "admin_id",
  ADMIN_TOKEN: "admin_token",
  ATTEMPTS_LEFT: "attempts_left",
  /** Per-round breakdown. TEAM_SCORE is now always a number. */
  ROUND_SCORES: "round_scores",
  TOTAL_ROUNDS: "total_rounds",
  /** [{ team_id, team_name, score, rank }, ...] */
  LEADERBOARD: "leaderboard",
  /** Seconds left on a login lockout (see Login.LOCKED). */
  RETRY_AFTER: "retry_after",
} as const;

export const Login = {
  LOGIN: "login",
  DENIED: "denied",
  ACCEPTED: "accepted",
  LOGOUT: "logout",
  /** Account temporarily locked after repeated failures — not a bad password. */
  LOCKED: "locked",
} as const;

export const NameStatus = {
  AVAILABLE: 0,
  TAKEN: 1,
  DNE: -1,
  LOCKED: 2,
} as const;

export const Responses = {
  LOGIN_RESPONSE: "login_response",
  LOGOUT_RESPONSE: "logout_response",
  GAME_STATE_RESPONSE: "game_state_response",
  TEAM_STATE_RESPONSE: "team_state_response",
  GAMEPLAY_RESPONSE: "gameplay_response",
  ADMIN_RESPONSE: "admin_response",
  /** Sent for malformed frames, unauthenticated requests, rate limiting and
   * out-of-turn prompts. Was missing here, so those frames were dropped. */
  ERROR_RESPONSE: "error_response",
} as const;

export const GameState = {
  LOGIN_PERIOD: 0,
  PREGAME: 1,
  GAME_RUNNING: 2,
  COUNTDOWN: 3,
  GAME_OVER: 4,
  ROUND_OVER: 6,
} as const;

export const TeamState = {
  WAITING: 0,
  JOINED: 2,
  LEFT: 3,
  PLAYING: 4,
  DONE: 5,
} as const;

export const GamePlay = {
  NOT_PROMPTED: 0,
  PROMPTED: 1,
  RECEIVED: 2,
  NOT_RECEIVED: 3,
  IMAGE_IN: 4,
  PROMPT_OUT: 5,
  INVALID_PROMPT: 6,
  OUT_OF_CHANCES: 7,
} as const;

export type ServerMessage = Record<string, unknown>;

/** Total rounds the backend plays (`GameServer.total_rounds`). */
export const TOTAL_ROUNDS = 5;

/**
 * Base URL of the FastAPI backend.
 * Override with VITE_API_BASE_URL (e.g. https://dwr-backend.example.com).
 * Empty string means "same origin" — useful when the API is reverse-proxied.
 */
export function getApiBaseUrl(): string {
  const configured = import.meta.env.VITE_API_BASE_URL as string | undefined;
  if (configured !== undefined && configured !== "") return configured.replace(/\/$/, "");
  if (typeof window !== "undefined" && window.location.hostname === "localhost") {
    return "http://localhost:8000";
  }
  return "";
}

export function getWebSocketUrl(): string {
  const base = getApiBaseUrl();
  if (base) return `${base.replace(/^http/, "ws")}/ws`;
  if (typeof window === "undefined") return "";
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws`;
}
