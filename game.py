import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, Depends, WebSocketDisconnect, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from enumerations import JSONFields, Login, Responses, GameState, TeamState, GamePlay
from models.server import GameServer
from paths import CLIENT_HTML, GENERATED_IMAGE_DIR, STATE_FILE, STATIC_DIR
from roster import load_roster
from services.ai_handling import (
    close_http_client, provider_status, shutdown_scoring_pool, warm_up_model,
)

# An invalid LOG_LEVEL used to raise ValueError from basicConfig at import
# time, i.e. the app refused to boot because of a typo in an env var.
_requested_level = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
if _requested_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}:
    _requested_level = "INFO"

logging.basicConfig(
    level=_requested_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("dwr.game")

# --- Websocket abuse limits -------------------------------------------
# One misbehaving (or malicious) client should not be able to monopolise the
# single event loop that serves every other player.
MAX_WS_FRAME_BYTES = int(os.getenv("MAX_WS_FRAME_BYTES", str(64 * 1024)))
MAX_LOGIN_ATTEMPTS_PER_SOCKET = int(os.getenv("MAX_LOGIN_ATTEMPTS_PER_SOCKET", "20"))
MIN_SECONDS_BETWEEN_LOGINS = float(os.getenv("MIN_SECONDS_BETWEEN_LOGINS", "0.5"))

# --- Game clock -------------------------------------------------------
# Environment-configurable so a smoke test doesn't have to sit through the
# real 90-second turns (a full 5-round game at production timings runs for
# roughly 40 minutes). Leave these unset for a real event.
TOTAL_ROUNDS = int(os.getenv("TOTAL_ROUNDS", "5"))
TIME_PER_ROUND = float(os.getenv("TIME_PER_ROUND", "90"))
TURN_TIMEOUT = float(os.getenv("TURN_TIMEOUT", "30"))
ROUND_PENALTY = float(os.getenv("ROUND_PENALTY", "5"))
ROUND_BREAK_SECONDS = float(os.getenv("ROUND_BREAK_SECONDS", "10"))
COUNTDOWN_SECONDS = float(os.getenv("COUNTDOWN_SECONDS", "5"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Load the CLIP weights in the background at startup instead of letting
    # the first team to finish round 1 pay the one-off model-load cost inside
    # a live round. Failures are logged, not fatal -- /health must still come
    # up so the platform's readiness probe passes.
    async def _warm():
        try:
            await asyncio.to_thread(warm_up_model)
            logger.info("CLIP image-similarity model loaded and ready.")
        except Exception as exc:
            logger.error(
                "Could not pre-load the CLIP model (%s). Scoring will retry on first use.", exc
            )

    warm_task = asyncio.create_task(_warm())
    try:
        yield
    finally:
        warm_task.cancel()
        await close_http_client()
        shutdown_scoring_pool()


app = FastAPI(lifespan=lifespan)

# CORS origins are configurable per deployment (Azure App Service, Krutrim
# Cloud, local server all serve the frontend from different hostnames).
_default_origins = "http://localhost:8080,http://localhost:5173,http://127.0.0.1:8080"
_cors_env = os.getenv("CORS_ALLOWED_ORIGINS")
CORS_ALLOWED_ORIGINS = [origin.strip() for origin in (_cors_env or _default_origins).split(",") if origin.strip()]
if not _cors_env:
    logger.warning(
        "CORS_ALLOWED_ORIGINS is not set; falling back to localhost-only defaults (%s). "
        "Set this environment variable to your deployed frontend origin(s) before going live.",
        CORS_ALLOWED_ORIGINS,
    )
if "*" in CORS_ALLOWED_ORIGINS:
    # allow_credentials=True with a wildcard origin is rejected by browsers
    # anyway, and would be an open door if it weren't. Fail loudly instead of
    # shipping a config that silently doesn't work.
    raise RuntimeError(
        "CORS_ALLOWED_ORIGINS must list explicit origins; '*' is not permitted "
        "because the API is served with credentials enabled."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Admin-Token"],
)

if os.path.exists(STATE_FILE):
    logger.warning(
        "%s already exists at startup. If this is a brand-new event, the game "
        "will resume from a stale checkpoint (previous rounds' scores) instead "
        "of starting fresh. Delete this file before running a new event, or "
        "keep it only when intentionally recovering from a crash mid-event.",
        STATE_FILE,
    )

# --- Roster -----------------------------------------------------------
# Loaded from Supabase, with a local cache so a Supabase outage on the
# morning of the event cannot stop the app from booting. See roster.py for
# the fallback order and the environment variables that control it.
#
# Credentials still live only in memory once loaded: passwords are hashed
# inside GameServer at construction and are never held in plain text after
# that point.
MAX_MEMBERS_PER_TEAM = int(os.getenv("MAX_MEMBERS_PER_TEAM", "4"))

try:
    _roster = load_roster(max_members_per_team=MAX_MEMBERS_PER_TEAM)
except Exception as exc:
    logger.critical("Refusing to start: %s", exc)
    raise

_team_names = [
    os.getenv(f"TEAM_NAME_{i}", f"Team {i + 1}") for i in range(_roster.team_count)
]

try:
    server = GameServer(
        server_id=0,
        max_teams=_roster.team_count,
        max_members_per_team=MAX_MEMBERS_PER_TEAM,
        team_names=_team_names,
        player_data=_roster.player_data,
        admin_data=_roster.admin_data,
    )
except ValueError as exc:
    logger.critical("Refusing to start: roster configuration is invalid: %s", exc)
    raise

server.total_rounds = TOTAL_ROUNDS
server.roster_source = _roster.source

logger.info(
    "Server ready: %s players across %s teams (roster source: %s).",
    len(server.players), len(server.teams), _roster.source,
)
if _roster.source != "supabase":
    logger.warning(
        "Roster did NOT come from Supabase this boot (source: %s). Confirm this is "
        "intended before the event starts.", _roster.source,
    )

# StaticFiles(directory="static") resolved against the *current working
# directory*, which is not the project root under Azure App Service's
# startup.sh or a systemd unit without WorkingDirectory= -- the app then
# raised "Directory 'static' does not exist" at import and never booted.
os.makedirs(GENERATED_IMAGE_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def save_game_checkpoint(completed_round: int, round_data: dict, inactive_teams: set):
    data = {
        "last_completed_round": completed_round,
        "rounds": {},
        "inactive_teams": sorted(inactive_teams),
    }

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                existing_data = json.load(f)
            if isinstance(existing_data, dict):
                data["rounds"] = existing_data.get("rounds", {}) or {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Existing %s is unreadable (%s); starting a fresh checkpoint history.", STATE_FILE, exc)

    data["rounds"][str(completed_round)] = round_data
    data["last_completed_round"] = completed_round

    temp_file = f"{STATE_FILE}.tmp"
    try:
        with open(temp_file, "w") as f:
            json.dump(data, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, STATE_FILE)
    except OSError as exc:
        # A checkpoint write failure must not abort the round for every team.
        logger.error("Could not write checkpoint %s: %s", STATE_FILE, exc)


def load_game_checkpoint():
    """Return (next_round_index, rounds_history, inactive_team_ids)."""
    if not os.path.exists(STATE_FILE):
        return 0, {}, set()

    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("checkpoint is not a JSON object")

        last_round = data.get("last_completed_round", -1)
        if not isinstance(last_round, int):
            raise ValueError("last_completed_round is not an integer")

        rounds_history = data.get("rounds", {}) or {}
        # inactive_teams was written to the checkpoint but never read back, so
        # a resumed game silently forgot which teams had not turned up.
        inactive = set(data.get("inactive_teams", []) or [])
        return last_round + 1, rounds_history, inactive
    except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
        logger.warning("Could not read checkpoint %s (%s); starting from round 0.", STATE_FILE, exc)
        return 0, {}, set()


def get_game_server():
    return server


async def start_games(game: GameServer, time_per_round, timeout, penalty):
    """Run every active team through the remaining rounds, then finish.

    Wrapped end-to-end: this coroutine is launched with create_task() and
    nothing awaits its result, so an exception escaping it used to vanish
    (asyncio only reports it when the task is garbage collected). The visible
    symptom was the worst possible one at an event -- the game froze in
    GAME_RUNNING forever, no team was ever scored, and no GAME_OVER frame was
    sent to anyone. The game is now always driven to a terminal state.
    """
    try:
        await _play_all_rounds(game, time_per_round, timeout, penalty)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Game loop failed; finishing the game with the scores collected so far.")
    finally:
        try:
            await _finish_game(game)
        except Exception:
            logger.exception("Failed to broadcast game over.")


async def _play_all_rounds(game: GameServer, time_per_round, timeout, penalty):
    start_round_num, rounds_history, checkpoint_inactive = load_game_checkpoint()

    active_teams = []
    inactive_team_ids = set(checkpoint_inactive)

    for team in game.teams:
        if len(team.connected_sockets) > 0:
            team.team_state = TeamState.PLAYING
            team.rotation_order = list(team.members)
            active_teams.append(team)
            game.participating_team_ids.add(team.id)

            team.score = []
            for r in range(start_round_num):
                score = rounds_history.get(str(r), {}).get(str(team.id), 0)
                team.score.append(score)
        else:
            team.team_state = TeamState.DONE
            team.score = []
            inactive_team_ids.add(team.id)

    if not active_teams:
        logger.warning("No teams had a connected player when the game started; nothing to play.")
        return

    total_rounds = game.total_rounds
    if start_round_num >= total_rounds:
        logger.warning(
            "Checkpoint says round %s of %s is already complete; going straight to results.",
            start_round_num, total_rounds,
        )
        return

    for round_num in range(start_round_num, total_rounds):
        game.current_round = round_num + 1

        # return_exceptions=True so one team's failure cannot cancel every
        # other team's round. Team.run_round already guards itself, so this
        # is belt-and-braces -- but the cost of being wrong here is the whole
        # event, so both layers stay.
        round_tasks = [
            asyncio.create_task(team.run_round(round_num, time_per_round, timeout, penalty))
            for team in active_teams
        ]
        results = await asyncio.gather(*round_tasks, return_exceptions=True)

        round_scores = []
        for team, result in zip(active_teams, results):
            if isinstance(result, BaseException):
                logger.error("Team %s round %s raised %r; scoring 0.", team.id, round_num + 1, result)
                round_scores.append(0.0)
            else:
                round_scores.append(result)

        round_data = {str(team.id): score for team, score in zip(active_teams, round_scores)}
        logger.info("Round %s complete for all teams: %s", round_num + 1, round_data)

        save_game_checkpoint(round_num, round_data, inactive_team_ids)

        announcements = [
            team.announce_to_team({
                JSONFields.TYPE: Responses.GAME_STATE_RESPONSE,
                JSONFields.GAME_STATE: GameState.ROUND_OVER,
                JSONFields.ROUND: round_num + 1,
                JSONFields.TOTAL_ROUNDS: total_rounds,
                JSONFields.ROUND_SCORE: round_data[str(team.id)],
                JSONFields.TEAM_SCORE: game.team_total(team),
                JSONFields.ROUND_SCORES: list(team.score),
                JSONFields.TIME: ROUND_BREAK_SECONDS,
            })
            for team in active_teams
        ]
        await asyncio.gather(*announcements, return_exceptions=True)
        await asyncio.sleep(ROUND_BREAK_SECONDS)

    logger.info("All teams finished the game")


async def _finish_game(game: GameServer):
    """Mark every team done, compute the leaderboard, and tell everyone."""
    for team in game.teams:
        team.team_state = TeamState.DONE
        game.scores[team.id] = game.team_total(team)

    game.game_state = GameState.GAME_OVER
    leaderboard = game.leaderboard()
    top3 = [(entry["team_id"], entry["score"]) for entry in leaderboard[:3]]

    logger.info("Final leaderboard: %s", leaderboard)

    done_announcements = [
        team.announce_to_team({
            JSONFields.TYPE: Responses.TEAM_STATE_RESPONSE,
            JSONFields.TEAM_STATE: TeamState.DONE,
        })
        for team in game.teams
    ]
    await asyncio.gather(*done_announcements, return_exceptions=True)

    over_announcements = [
        team.announce_to_team({
            JSONFields.TYPE: Responses.GAME_STATE_RESPONSE,
            JSONFields.GAME_STATE: GameState.GAME_OVER,
            # Always a number. The per-round breakdown has its own key --
            # previously this field held an int at ROUND_OVER and a list at
            # GAME_OVER, so every client had to type-sniff it.
            JSONFields.TEAM_SCORE: game.team_total(team),
            JSONFields.ROUND_SCORES: list(team.score) if isinstance(team.score, list) else [],
            JSONFields.TEAM_RANK: team.rank,
            JSONFields.TOP3: top3,
            JSONFields.LEADERBOARD: leaderboard,
        })
        for team in game.teams
    ]
    await asyncio.gather(*over_announcements, return_exceptions=True)


async def _cleanup_connection(game: GameServer, user_id, team_id, client_type, admin_token):
    """Guaranteed cleanup for a websocket connection, regardless of *why*
    the connection loop ended (clean disconnect, malformed payload,
    unexpected exception, ...)."""
    if user_id is None:
        return

    if client_type == "admin":
        game.connected_admins.discard(user_id)
        game.connected_sockets.pop(user_id, None)
        if admin_token is not None:
            game.admin_tokens.pop(admin_token, None)
        else:
            game.revoke_admin_tokens_for(user_id)
        return

    if team_id is None:
        game.connected_players.discard(user_id)
        game.connected_sockets.pop(user_id, None)
        return

    # A player may have re-authenticated on a *new* socket while this one was
    # still closing. Only tear down the session if this socket is still the
    # registered one -- otherwise the stale socket's cleanup would log the
    # player straight back out of their fresh session.
    team = game.teams[team_id]
    current = game.connected_sockets.get(user_id)
    replaced = current is not None and current is not team.connected_sockets.get(user_id)
    if replaced:
        logger.info("Skipping cleanup for user %s: a newer session is active.", user_id)
        return

    game.connected_players.discard(user_id)
    game.connected_sockets.pop(user_id, None)
    team.connected_sockets.pop(user_id, None)

    if user_id in game.connected_teams[team_id]:
        game.connected_teams[team_id].remove(user_id)

    await team.announce_to_team({
        JSONFields.TYPE: Responses.TEAM_STATE_RESPONSE,
        JSONFields.TEAM_STATE: TeamState.LEFT,
        JSONFields.USERNAME: [
            game.players[uid].username for uid in game.connected_teams[team_id] if uid in game.players
        ],
    })


@app.websocket("/ws")
async def game_endpoint(websocket: WebSocket, game: GameServer = Depends(get_game_server)):
    await websocket.accept()
    current_user_id = None
    current_team_id = None
    current_client_type = None  # Track whether this is an admin or player connection
    current_admin_token = None

    login_attempts = 0
    last_login_at = 0.0

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                raise
            except (RuntimeError, KeyError):
                # Starlette raises these when the socket is already closing or
                # a non-text frame arrives.
                raise WebSocketDisconnect(code=1006)

            # Bound the work a single frame can cause before parsing it. Without
            # this, a client can stream an arbitrarily large body and json.loads
            # will happily allocate it on the single shared event loop.
            if len(raw) > MAX_WS_FRAME_BYTES:
                logger.warning("Rejecting oversized websocket frame (%s bytes).", len(raw))
                await websocket.send_json({
                    JSONFields.TYPE: Responses.ERROR_RESPONSE,
                    JSONFields.MESSAGE: "Message too large.",
                })
                continue

            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                logger.info("Ignoring malformed websocket payload: %s", exc)
                await websocket.send_json({
                    JSONFields.TYPE: Responses.ERROR_RESPONSE,
                    JSONFields.MESSAGE: "Malformed request.",
                })
                continue

            if not isinstance(data, dict):
                await websocket.send_json({
                    JSONFields.TYPE: Responses.ERROR_RESPONSE,
                    JSONFields.MESSAGE: "Expected a JSON object.",
                })
                continue

            message_type = data.get(JSONFields.TYPE)

            # Login routing
            if message_type == Login.LOGIN:
                now = time.time()
                login_attempts += 1
                # Password verification is deliberately expensive (PBKDF2).
                # Rate-limit per socket so a single client cannot pin a CPU
                # core -- and therefore stall every other player -- by
                # spamming login frames.
                if login_attempts > MAX_LOGIN_ATTEMPTS_PER_SOCKET:
                    await websocket.send_json({
                        JSONFields.TYPE: Responses.ERROR_RESPONSE,
                        JSONFields.MESSAGE: "Too many login attempts on this connection.",
                    })
                    break
                if now - last_login_at < MIN_SECONDS_BETWEEN_LOGINS:
                    await websocket.send_json({
                        JSONFields.TYPE: Responses.ERROR_RESPONSE,
                        JSONFields.MESSAGE: "Slow down and try again.",
                    })
                    continue
                last_login_at = now

                response = await game.check_login(data, websocket)

                if response.get(JSONFields.TYPE) == Responses.ADMIN_RESPONSE:
                    if response.get(JSONFields.AUTHORISED) == Login.ACCEPTED:
                        current_user_id = response.get(JSONFields.ADMIN_ID)
                        current_admin_token = response.get(JSONFields.ADMIN_TOKEN)
                        current_client_type = "admin"
                        current_team_id = None
                    await websocket.send_json(response)
                else:
                    if response.get(JSONFields.AUTHORISED) == Login.ACCEPTED:
                        current_user_id = response.get(JSONFields.USER_ID)
                        current_team_id = game.players[current_user_id].team_id
                        current_client_type = "player"

                        await websocket.send_json(response)

                        await game.teams[current_team_id].announce_to_team({
                            JSONFields.TYPE: Responses.TEAM_STATE_RESPONSE,
                            JSONFields.TEAM_STATE: TeamState.JOINED,
                            JSONFields.USERNAME: [
                                game.players[uid].username
                                for uid in game.connected_teams[current_team_id]
                                if uid in game.players
                            ],
                        })
                    else:
                        await websocket.send_json(response)

            # Authenticated Session Management Loop
            elif current_user_id is not None:
                if message_type == Login.LOGOUT:
                    response = game.check_logout(
                        id=current_user_id, client_type=current_client_type, token=current_admin_token
                    )
                    await websocket.send_json(response)

                    if current_client_type == "player" and response.get(JSONFields.AUTHORISED) == Login.ACCEPTED:
                        await game.teams[current_team_id].announce_to_team({
                            JSONFields.TYPE: Responses.TEAM_STATE_RESPONSE,
                            JSONFields.TEAM_STATE: TeamState.LEFT,
                            JSONFields.USERNAME: [
                                game.players[uid].username
                                for uid in game.connected_teams[current_team_id]
                                if uid in game.players
                            ],
                        })

                    current_user_id = None
                    current_team_id = None
                    current_client_type = None
                    current_admin_token = None

                elif current_client_type == "player" and game.game_state in (
                    GameState.GAME_RUNNING, GameState.COUNTDOWN
                ):
                    if message_type == GamePlay.PROMPT_OUT:
                        team = game.teams[current_team_id]
                        if current_user_id == team.current_turn_uid:
                            # Bound the queue defensively: only the player
                            # whose turn it is can reach this, but a scripted
                            # client could still flood it during its own turn.
                            if team.input_queue.qsize() < 32:
                                await team.input_queue.put(data)
                        else:
                            await websocket.send_json({
                                JSONFields.TYPE: Responses.ERROR_RESPONSE,
                                JSONFields.MESSAGE: "It is not your turn.",
                            })
            else:
                await websocket.send_json({
                    JSONFields.TYPE: Responses.ERROR_RESPONSE,
                    JSONFields.MESSAGE: "Not authenticated.",
                })

    except WebSocketDisconnect:
        logger.info("Client disconnected (user_id=%s, type=%s)", current_user_id, current_client_type)
    except Exception:
        logger.exception("Unexpected error in websocket loop (user_id=%s, type=%s)", current_user_id, current_client_type)
    finally:
        try:
            await _cleanup_connection(game, current_user_id, current_team_id, current_client_type, current_admin_token)
        except Exception:
            logger.exception("Error while cleaning up connection for user_id=%s", current_user_id)


def verify_admin_session(
    x_admin_token: str = Header(None, alias="X-Admin-Token"),
    game: GameServer = Depends(get_game_server),
):
    """Verify that the request carries a valid, currently active admin session
    token (issued at websocket login), not just a guessable admin id."""
    if not x_admin_token:
        raise HTTPException(status_code=401, detail="Missing administrative credentials.")

    # Resolved through the injected server rather than the module-level
    # global, so the dependency honours any dependency_overrides in tests and
    # cannot drift from the server the rest of the request uses.
    admin_id = game.resolve_admin_token(x_admin_token)
    if admin_id is None:
        raise HTTPException(status_code=403, detail="Unauthorized: Admin session not found or active.")

    return admin_id


async def _run_countdown_then_start(game: GameServer, time_per_round, timeout, penalty):
    await game.announce_to_players({
        JSONFields.TYPE: Responses.GAME_STATE_RESPONSE,
        JSONFields.GAME_STATE: GameState.COUNTDOWN,
        JSONFields.TIME: COUNTDOWN_SECONDS,
    })
    await asyncio.sleep(COUNTDOWN_SECONDS)

    game.game_state = GameState.GAME_RUNNING
    await game.announce_to_players({
        JSONFields.TYPE: Responses.GAME_STATE_RESPONSE,
        JSONFields.GAME_STATE: GameState.GAME_RUNNING,
        JSONFields.TOTAL_ROUNDS: game.total_rounds,
    })

    await start_games(game, time_per_round=time_per_round, timeout=timeout, penalty=penalty)


def _log_task_exception(task: asyncio.Task):
    """create_task() results are never awaited here, so without this an
    exception is only surfaced when the task is garbage collected."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Background game task failed: %r", exc, exc_info=exc)


@app.post("/admin/rungame")
async def force_start_game(
    admin_id: int = Depends(verify_admin_session),
    game: GameServer = Depends(get_game_server),
):
    # Block starting if the game is already out of the login phase
    if game.game_state != GameState.LOGIN_PERIOD:
        raise HTTPException(status_code=400, detail="Game has already started or concluded.")

    game.game_state = GameState.COUNTDOWN
    game.countdown_end_time = time.time() + COUNTDOWN_SECONDS
    logger.info("Admin %s started the game.", admin_id)

    # The countdown used to be awaited inline, so this endpoint blocked for
    # five seconds before responding -- long enough for an admin dashboard
    # with a short client timeout to report a failure for a game that did in
    # fact start.
    task = asyncio.create_task(
        _run_countdown_then_start(
            game,
            time_per_round=TIME_PER_ROUND,
            timeout=TURN_TIMEOUT,
            penalty=ROUND_PENALTY,
        )
    )
    task.add_done_callback(_log_task_exception)

    return {"status": "success", "message": "Game countdown initiated successfully."}


@app.get("/admin/dashboard")
async def get_global_dashboard(
    admin_id: int = Depends(verify_admin_session),
    game: GameServer = Depends(get_game_server),
):
    connected_teams = sum(1 for team in game.teams if len(team.connected_sockets) > 0)
    return {
        "game_state": game.game_state,
        "total_connected_players": len(game.connected_players),
        "team_names": [
            {
                "team_id": team.id,
                "team_name": team.team_name,
                # Assigned capacity vs active connections
                "total_members": team.max_members,
                "assigned_members": len(team.members),
                "connected_members": len(team.connected_sockets),
                "score": game.team_total(team),
                "round_scores": list(team.score) if isinstance(team.score, list) else [],
                "rank": team.rank,
                "members": [
                    game.players[uid].username if uid in game.players else str(uid)
                    for uid in team.members
                ],
            }
            for team in game.teams
        ],
        "connected_teams": connected_teams,
        "total_teams": len(game.teams),
        "current_round": game.current_round,
        "total_rounds": game.total_rounds,
        "leaderboard": game.leaderboard(),
        # So an operator can confirm at a glance that the roster came from
        # Supabase and not from a stale cache or the placeholder set.
        "roster_source": getattr(game, "roster_source", "unknown"),
        # Circuit-breaker state per image provider, so a failover is visible
        # on the dashboard rather than something to infer from the logs.
        "image_providers": provider_status(),
    }


@app.get("/health")
async def health_check():
    """Lightweight liveness/readiness probe for Azure App Service, Krutrim
    Cloud, or a local reverse proxy/load balancer. Deliberately unauthenticated
    and cheap -- it must not depend on the game state or AI services."""
    return {"status": "ok"}


@app.get("/")
async def read_root():
    return FileResponse(CLIENT_HTML)
