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
from final_score import build_phase2_roster
from models.server import GameServer
from paths import CLIENT_HTML, GENERATED_IMAGE_DIR, STATE_DIR, STATE_FILE, STATIC_DIR
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

if os.getenv("TURN_TIMEOUT") is not None:
    logger.warning(
        "TURN_TIMEOUT is set (%s) but nothing reads it -- Team._run_round bounds "
        "a turn with TIME_PER_ROUND alone. If you were trying to change how long "
        "a player gets, change TIME_PER_ROUND: despite its name that is the "
        "per-TURN budget, and a round is MAX_MEMBERS_PER_TEAM of them.",
        os.getenv("TURN_TIMEOUT"),
    )

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
# TURN_TIMEOUT is threaded through start_games -> _play_all_rounds ->
# Team.run_round and then never read: Team._run_round bounds a turn with
# TIME_PER_ROUND alone. The parameter is kept because the signature is public
# enough to be worth not breaking, but the variable is a trap -- someone
# tuning "how long a turn lasts" will reach for the one called TURN_TIMEOUT,
# set it, see no change, and conclude the config is not being read at all.
# Warn if it has been set deliberately (see below).
TURN_TIMEOUT = float(os.getenv("TURN_TIMEOUT", "30"))
ROUND_PENALTY = float(os.getenv("ROUND_PENALTY", "5"))
ROUND_BREAK_SECONDS = float(os.getenv("ROUND_BREAK_SECONDS", "10"))
COUNTDOWN_SECONDS = float(os.getenv("COUNTDOWN_SECONDS", "5"))

# Headroom on top of (TIME_PER_ROUND x MAX_MEMBERS_PER_TEAM) before a round is
# treated as hung and cancelled. It has to absorb everything run_round can
# legitimately spend BEYOND the raw turn budgets:
#
#   * up to 10s per turn waiting for a player who is offline at turn start
#   * up to team.IMAGE_GRACE_SECONDS (15s) per turn for an in-flight generation
#     that began while there was still time on the clock
#
# which is ~25s x 4 members = ~100s worst case. At the defaults (90s turns, 4
# members) that puts a fully-degraded-but-legitimate round at ~460s against a
# 360s raw turn budget, so 120s of slack would leave only 20s of margin -- 4%,
# on a number whose failure mode is every team in the round scoring 0.
#
# 180s gives ~80s of real margin and still catches a genuine hang inside 9
# minutes. The asymmetry is the point: waiting an extra minute costs nothing,
# cancelling a slow-but-working round costs the event. Raise it, don't lower
# it, unless you have measured a real round.
ROUND_SLACK_SECONDS = float(os.getenv("ROUND_SLACK_SECONDS", "180"))

# --- Phase 2 handoff --------------------------------------------------
# The top 50% of the final standings advance to the CTFd-hosted round 2. At
# game over final_score.py writes two files next to the checkpoint: a CSV in
# CTFd's bulk-import shape (name,email,password) holding exactly the teams
# that qualified, and their phase 1 scores so those points can be carried
# over. The generated passwords come back in memory too, so each team can be
# shown its own on the results screen.
#
# Both files contain credentials -- keep them out of version control and off
# any world-readable path (DWR_STATE_DIR).
#
# TODO: replace the placeholder with the real CTFd URL before the event
# (set PHASE2_CTFD_URL rather than editing this default).
PHASE2_CTFD_URL = os.getenv("PHASE2_CTFD_URL", "https://ctfd.example.com/dark-web-rises")
PHASE2_ROSTER_CSV = os.path.join(STATE_DIR, "roster_cache_phase2.csv")
PHASE2_SCORES_FILE = os.path.join(STATE_DIR, "phase1_scores.json")


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

    # anyio's default thread limiter is 40 tokens, and TWO unrelated workloads
    # draw on it at the same instant:
    #
    #   * httpx resolves DNS via anyio.to_thread.run_sync, at up to
    #     MAX_CONCURRENT_IMAGE_REQUESTS (40) concurrent generations;
    #   * Starlette's StaticFiles does its stat/read on the same pool, and with
    #     IMAGE_DELIVERY=url all 175 players fetch their image over that mount.
    #
    # So the burst of image fetches at a turn boundary directly delays DNS for
    # the next wave of generations, invisibly. The load-test harness already
    # raises this; the server never did.
    try:
        import anyio.to_thread
        anyio.to_thread.current_default_thread_limiter().total_tokens = 200
        logger.info("anyio thread limiter raised to 200.")
    except Exception:
        logger.warning("Could not raise the anyio thread limiter.", exc_info=True)

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

# --- Roster size guard ------------------------------------------------
# The failure this exists to catch is silent and total.
#
# roster.py falls back to the local cache when Supabase is unreachable, which
# is right -- an outage on the morning of the event must not stop the app from
# booting. But the cache is a snapshot, and in the Docker deployment it is
# mounted read-only, so the app cannot refresh it even when Supabase *is*
# reachable (see tools/roster_build.py). A stale cache therefore boots
# perfectly: "Server ready: 600 players across 150 teams", health check green,
# no error anywhere. The only symptom is that the attendees who signed up after
# the snapshot cannot log in, and you learn that from the queue at the desk.
#
# Setting EXPECTED_TEAMS / EXPECTED_PLAYERS converts that into a refusal to
# start, or at minimum a warning nobody can miss. Left unset the behaviour is
# unchanged, so this cannot break a deployment that does not opt in.
def _optional_int(name):
    """Parse an optional integer setting, treating blank and junk as unset.

    int(os.getenv(name, "0")) is not good enough here. docker-compose.yml
    passes `EXPECTED_TEAMS=${EXPECTED_TEAMS:-}`, which substitutes to an
    EMPTY STRING when the variable is not set in .env -- and int("") raises
    ValueError at import time, before logging is useful, taking down every
    deployment that had not opted in. That is the precise failure this guard
    exists to prevent, reintroduced by the guard itself.
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "%s is set to %r, which is not a number; ignoring it. The roster "
            "size check is disabled until it is a valid integer.", name, raw,
        )
        return 0


EXPECTED_TEAMS = _optional_int("EXPECTED_TEAMS")
EXPECTED_PLAYERS = _optional_int("EXPECTED_PLAYERS")
# Default strict: if you have told the app how big the event is and the roster
# disagrees, that is not a detail to warn about and continue past.
ROSTER_SIZE_STRICT = os.getenv("ROSTER_SIZE_STRICT", "true").strip().lower() != "false"

_size_problems = []
if EXPECTED_TEAMS and len(server.teams) != EXPECTED_TEAMS:
    _size_problems.append(
        f"EXPECTED_TEAMS={EXPECTED_TEAMS} but the roster has {len(server.teams)} teams"
    )
if EXPECTED_PLAYERS and len(server.players) != EXPECTED_PLAYERS:
    _size_problems.append(
        f"EXPECTED_PLAYERS={EXPECTED_PLAYERS} but the roster has "
        f"{len(server.players)} players"
    )

if _size_problems:
    _detail = (
        "ROSTER SIZE MISMATCH\n  "
        + "\n  ".join(_size_problems)
        + f"\n\nRoster source: {_roster.source}."
        + (
            "\nThis roster came from the local CACHE, not Supabase, so it is a "
            "snapshot and\nanyone who signed up since it was taken is missing. "
            "Refresh it from the host:\n  python tools/roster_build.py refresh\n"
            "  python tools/roster_build.py check --expect-teams "
            f"{EXPECTED_TEAMS or len(server.teams)}"
            if _roster.source != "supabase" else
            "\nThis roster DID come from Supabase, so the expectation is what is "
            "wrong,\nnot the data -- correct EXPECTED_TEAMS/EXPECTED_PLAYERS."
        )
        + "\n\nSet ROSTER_SIZE_STRICT=false to start anyway (attendees missing from "
          "the\nroster will not be able to log in)."
    )
    if ROSTER_SIZE_STRICT:
        logger.critical("Refusing to start: %s", _detail)
        raise RuntimeError(_detail)
    logger.error("%s", _detail)
elif EXPECTED_TEAMS or EXPECTED_PLAYERS:
    logger.info(
        "Roster size confirmed against EXPECTED_TEAMS/EXPECTED_PLAYERS: %s teams, "
        "%s players.", len(server.teams), len(server.players),
    )

# --- How long the event will actually take ----------------------------
# TIME_PER_ROUND is, despite the name, the budget for a single TURN -- see
# Team._run_round, which sets `turn_end_time = now + time_per_round` once per
# player. A round is therefore up to MAX_MEMBERS_PER_TEAM turns long. Reading
# the variable name alone, a moderator would reasonably plan for a 90-second
# round and a 8-minute game, and be an hour out.
#
# Rather than rename the variable (which would silently ignore every existing
# .env that sets the old one), state the derived number at startup so the
# schedule comes from arithmetic instead of from an assumption.
_max_round_seconds = TIME_PER_ROUND * MAX_MEMBERS_PER_TEAM + ROUND_BREAK_SECONDS
_max_game_seconds = COUNTDOWN_SECONDS + _max_round_seconds * TOTAL_ROUNDS
logger.info(
    "Round timing: TIME_PER_ROUND=%.0fs is the budget PER TURN, so a round is up "
    "to %d turns x %.0fs = %.0fs. Worst case for %d rounds: %.0f minutes. Turns "
    "end early when a player submits, so the real figure is lower -- but plan "
    "the schedule against this one.",
    TIME_PER_ROUND, MAX_MEMBERS_PER_TEAM, TIME_PER_ROUND, _max_round_seconds,
    TOTAL_ROUNDS, _max_game_seconds / 60,
)
logger.info(
    "Round hang ceiling: a round is cancelled after %.0fs (%.0fs of turns + %.0fs "
    "slack). Cancelled teams score 0 for that round, so this must sit comfortably "
    "above a legitimately slow round -- raise ROUND_SLACK_SECONDS if it does not.",
    TIME_PER_ROUND * MAX_MEMBERS_PER_TEAM + ROUND_SLACK_SECONDS,
    TIME_PER_ROUND * MAX_MEMBERS_PER_TEAM, ROUND_SLACK_SECONDS,
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
        # Recorded so a resume can tell whether this checkpoint describes the
        # same *shape* of game. Round scores from a 5-round game mean something
        # different in a 1-round game -- different denominator for the phase 2
        # cut, different --max-phase1 for the final leaderboard -- so resuming
        # across a change to TOTAL_ROUNDS silently produces wrong standings.
        "total_rounds": TOTAL_ROUNDS,
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


# Resuming a checkpoint that says the game is already finished is almost never
# what anyone wants. Set this to opt into it anyway -- the one case where it is
# right is a crash between the final round and the game-over broadcast.
ALLOW_RESUME_COMPLETED = os.getenv("DWR_RESUME_COMPLETED", "").strip().lower() in {"1", "true", "yes"}


def checkpoint_status():
    """Is the checkpoint on disk safe to start a game from?

    Returns (ok: bool, message: str|None).

    This exists because of how badly the alternative fails. `_play_all_rounds`
    already noticed a finished checkpoint and returned early -- but `start_games`
    runs `_finish_game()` in a `finally`, so returning early does not abort the
    game, it *completes* it. The moderator presses Start Game and instantly gets
    a GAME_OVER broadcast, a phase 2 CSV, freshly generated CTFd passwords, and
    qualification handed to whichever teams happened to have a socket open.

    Nothing about that looks like an error. The dashboard shows a finished game.
    It is only wrong if you know no rounds were played.

    A leftover checkpoint is by far the likeliest cause -- the file survives
    `docker compose down`, because it is bind-mounted on purpose so the phase 2
    CSV is not lost. So the default is to refuse and say so, and resuming a
    finished game is opt-in.
    """
    if not os.path.exists(STATE_FILE):
        return True, None

    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("checkpoint is not a JSON object")
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        # Unreadable is not a reason to block: load_game_checkpoint() already
        # ignores it and starts from round 0, which is the safe outcome.
        logger.warning("Checkpoint %s is unreadable (%s); starting fresh.", STATE_FILE, exc)
        return True, None

    last_round = data.get("last_completed_round", -1)
    if not isinstance(last_round, int):
        return True, None

    recorded_rounds = data.get("total_rounds")
    if isinstance(recorded_rounds, int) and recorded_rounds != TOTAL_ROUNDS:
        return False, (
            f"The saved checkpoint is from a {recorded_rounds}-round game, but "
            f"TOTAL_ROUNDS is now {TOTAL_ROUNDS}. Scores from a "
            f"{recorded_rounds}-round game are not comparable in a "
            f"{TOTAL_ROUNDS}-round one, and the phase 2 cut would be made on "
            f"the wrong totals.\n"
            f"Delete {STATE_FILE} to start a fresh game."
        )

    if last_round + 1 >= TOTAL_ROUNDS and not ALLOW_RESUME_COMPLETED:
        return False, (
            f"The saved checkpoint says all {TOTAL_ROUNDS} round(s) are already "
            f"complete (last_completed_round={last_round}).\n"
            f"Starting now would play NOTHING and go straight to game over -- "
            f"broadcasting results, writing the phase 2 CSV and issuing CTFd "
            f"passwords to whoever is currently connected.\n"
            f"Delete {STATE_FILE} to start a fresh game, or set "
            f"DWR_RESUME_COMPLETED=true if you really are recovering a crash "
            f"that happened after the final round."
        )

    if last_round >= 0:
        logger.warning(
            "Starting from a checkpoint: rounds 0-%s are already scored, resuming "
            "at round %s of %s. If this is a NEW event, stop and delete %s.",
            last_round, last_round + 2, TOTAL_ROUNDS, STATE_FILE,
        )
    return True, None


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

    def _admit(team, joining_at_round):
        """Bring a team into play, back-filling the rounds it has missed."""
        team.team_state = TeamState.PLAYING
        team.rotation_order = list(team.members)
        team.score = []
        for r in range(start_round_num):
            team.score.append(rounds_history.get(str(r), {}).get(str(team.id), 0))
        # Rounds already played in THIS session, before they joined.
        while len(team.score) < joining_at_round:
            team.score.append(0.0)
        active_teams.append(team)
        game.participating_team_ids.add(team.id)
        inactive_team_ids.discard(team.id)

    for team in game.teams:
        if len(team.connected_sockets) > 0:
            _admit(team, joining_at_round=start_round_num)
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

        # Late admission.
        #
        # Membership used to be decided ONCE, about five seconds after the
        # moderator pressed Start. A team still mid-login at that instant --
        # entirely normal when 700 people are typing credentials off printed
        # cards -- was marked DONE and excluded from the leaderboard for the
        # whole event, with no way to add them back. The failure looked like a
        # frontend bug: those players eventually logged in and were shown a
        # results screen with a score of 0 while everyone else played.
        #
        # Re-checking each round means a team that arrives during round 1 plays
        # from round 2 with zeros behind it, rather than losing the event to a
        # five-second window.
        for team in game.teams:
            if team.id in game.participating_team_ids or not team.connected_sockets:
                continue
            _admit(team, joining_at_round=round_num)
            logger.info(
                "Team %s connected late; joining from round %s with %s zero-scored "
                "round(s) behind them.", team.id, round_num + 1, round_num,
            )
            # Their clients are sitting on a results or waiting screen, so tell
            # them the game is running before their first turn arrives.
            await team.announce_to_team({
                JSONFields.TYPE: Responses.GAME_STATE_RESPONSE,
                JSONFields.GAME_STATE: GameState.GAME_RUNNING,
                JSONFields.ROUND: round_num + 1,
                JSONFields.TOTAL_ROUNDS: total_rounds,
            })

        # return_exceptions=True so one team's failure cannot cancel every
        # other team's round. Team.run_round already guards itself, so this
        # is belt-and-braces -- but the cost of being wrong here is the whole
        # event, so both layers stay.
        round_tasks = [
            asyncio.create_task(team.run_round(round_num, time_per_round, timeout, penalty))
            for team in active_teams
        ]

        # Hard ceiling on the round.
        #
        # Without it the round ends only when the SLOWEST team's run_round
        # returns, and nothing bounded that: one team stuck behind a degraded
        # provider held all 700 players on a screen whose timer already read 0,
        # with no moderator override. A round is at most one turn per member
        # plus the grace windows inside run_round, so anything past that plus
        # slack is a hang, not slow play.
        #
        # Cancellation is a designed path now -- Team.run_round records the
        # zero on CancelledError so team.score stays aligned with the round
        # index -- and the isinstance(BaseException) handler below already
        # scores a timed-out team 0.
        round_deadline = time_per_round * game.max_members_per_team + ROUND_SLACK_SECONDS
        results = await asyncio.gather(
            *(asyncio.wait_for(t, timeout=round_deadline) for t in round_tasks),
            return_exceptions=True,
        )

        round_scores = []
        for team, result in zip(active_teams, results):
            if isinstance(result, BaseException):
                logger.error("Team %s round %s raised %r; scoring 0.", team.id, round_num + 1, result)
                round_scores.append(0.0)
            else:
                round_scores.append(result)

        round_data = {str(team.id): score for team, score in zip(active_teams, round_scores)}
        logger.info("Round %s complete for all teams: %s", round_num + 1, round_data)

        # Off the event loop: this re-serialises the whole history and fsyncs,
        # and on a bind-mounted state volume an fsync can stall for hundreds of
        # milliseconds. It runs at the exact moment 175 teams have just finished
        # scoring, on the single loop that serves all 700 players.
        await asyncio.to_thread(
            save_game_checkpoint, round_num, round_data, inactive_team_ids
        )

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
    """Mark every team done, compute the leaderboard, and tell everyone.

    Idempotent. This MUST run exactly once: it generates the Phase 2 CTFd
    passwords and writes roster_cache_phase2.csv, so a second pass would issue
    fresh passwords that no longer match the accounts already imported into
    CTFd. Two callers now exist (start_games' `finally` and /admin/abort), and
    on an abort both can fire, so the guard is load-bearing rather than
    defensive. Safe without a lock: nothing between here and the GAME_OVER
    assignment below awaits, so no other task can interleave.
    """
    if game.game_state == GameState.GAME_OVER:
        logger.info("_finish_game called again after game over; ignoring.")
        return

    for team in game.teams:
        team.team_state = TeamState.DONE
        game.scores[team.id] = game.team_total(team)

    game.game_state = GameState.GAME_OVER
    leaderboard = game.leaderboard()
    top3 = [(entry["team_id"], entry["score"]) for entry in leaderboard[:3]]

    logger.info("Final leaderboard: %s", leaderboard)

    # Phase 2 cut. Made on the *cumulative* final standings (final_score.py's
    # CLI defaults to a single round, which is not what qualification is
    # decided on), and only teams that actually took part are ranked --
    # leaderboard() already excludes the rest.
    # Wrapped: a failure here must not stop the game-over frame going out.
    qualified_ids = set()
    phase2_passwords = {}
    try:
        qualified, cutoff, phase2_passwords = await asyncio.to_thread(
            build_phase2_roster,
            PHASE2_ROSTER_CSV,
            PHASE2_SCORES_FILE,
            team_names={team.id: team.team_name for team in game.teams},
            scores={entry["team_id"]: entry["score"] for entry in leaderboard},
        )
        qualified_ids = set(qualified)
        # Retained on the server so check_login can serve these to a team that
        # reconnects after game over -- see GameServer.phase2_* . Without this
        # the credentials exist only in the single GAME_OVER broadcast below,
        # and a team that missed it has no way to recover them.
        game.phase2_qualified_ids = qualified_ids
        game.phase2_passwords = dict(phase2_passwords)
        game.phase2_url = PHASE2_CTFD_URL
        logger.info(
            "Phase 2: %s of %s teams qualified (cutoff %s). CTFd import written to "
            "%s, phase 1 scores to %s.",
            len(qualified_ids), len(leaderboard), cutoff,
            PHASE2_ROSTER_CSV, PHASE2_SCORES_FILE,
        )
    except Exception:
        logger.exception(
            "Could not build the phase 2 roster; no qualification will be announced."
        )

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
            # Phase 2 handoff. Built per team, not broadcast: the link and the
            # generated CTFd credentials only go to the team they belong to,
            # so nobody can read another team's password off the wire.
            JSONFields.QUALIFIED: team.id in qualified_ids,
            JSONFields.PHASE2_URL: PHASE2_CTFD_URL if team.id in qualified_ids else None,
            JSONFields.PHASE2_USERNAME: team.team_name if team.id in qualified_ids else None,
            JSONFields.PHASE2_PASSWORD: phase2_passwords.get(team.id),
        })
        for team in game.teams
    ]
    await asyncio.gather(*over_announcements, return_exceptions=True)


async def _cleanup_connection(game: GameServer, user_id, team_id, client_type, admin_token,
                              socket=None):
    """Guaranteed cleanup for a websocket connection, regardless of *why*
    the connection loop ended (clean disconnect, malformed payload,
    unexpected exception, ...).

    `socket` is the connection that is closing, and it is required to get this
    right -- see the identity check below.
    """
    if user_id is None:
        return

    if client_type == "admin":
        # Same identity check as the player path below, for the same reason.
        #
        # Without it, opening the dashboard in a second tab and closing it
        # empties connected_admins -- and resolve_admin_token gates on set
        # membership, so the FIRST tab's still-valid token starts 403ing. The
        # dashboard poll swallows that and keeps rendering stale numbers, Run
        # Game refuses, and logout does not help because check_logout also
        # requires membership. Only a hard reload recovers, which is not
        # something a moderator will work out with 700 people waiting.
        registered = game.connected_sockets.get(user_id)
        if socket is not None and registered is not None and registered is not socket:
            logger.info(
                "Skipping admin cleanup for %s: superseded by a newer admin session.",
                user_id,
            )
            # Still retire this session's own token; it is not the live one.
            if admin_token is not None:
                game.admin_tokens.pop(admin_token, None)
            return

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
    # still closing. Only tear down the session if THIS socket is still the
    # registered one -- otherwise the stale socket's cleanup logs the player
    # straight back out of their fresh session.
    #
    # That is exactly what used to happen. The check compared
    # `game.connected_sockets[uid]` against `team.connected_sockets[uid]`,
    # but check_login sets both to the same object, so the two were always
    # equal, `replaced` was always False, and the guard never fired once. The
    # symptom was subtle and awful: log in on a second device, close the
    # first, and the *second* device silently stops receiving frames -- it is
    # still "logged in" on screen with no socket registered anywhere on the
    # server. Reloading appeared to fix it, which sent everyone hunting in the
    # frontend.
    #
    # The comparison has to be against the closing socket's own identity, so
    # it is passed in. Without it there is nothing to compare and the only
    # safe behaviour is to skip cleanup and let the session expire naturally.
    team = game.teams[team_id]
    registered = game.connected_sockets.get(user_id)
    if socket is not None and registered is not None and registered is not socket:
        logger.info(
            "Skipping cleanup for user %s: this socket was superseded by a newer session.",
            user_id,
        )
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

            # Liveness probe reply. Handled FIRST, above the authentication
            # branches, because it has to work while another connection is
            # blocked waiting for it -- and because it is the one frame whose
            # whole value is arriving promptly. Routing it through the normal
            # authenticated path would put it behind the `current_user_id is
            # not None` check and the game-state gate below, and a probe that
            # is answered too late is indistinguishable from one that is not
            # answered at all: the account gets released and this player is
            # signed out of a session they were actively using.
            if message_type == Login.SESSION_PROBE_ACK:
                if current_user_id is not None:
                    game.note_session_probe_ack(current_user_id)
                continue

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
            await _cleanup_connection(
                game, current_user_id, current_team_id, current_client_type,
                current_admin_token, socket=websocket,
            )
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


# Strong references to background tasks.
#
# asyncio holds only a WEAK reference to a running task, and add_done_callback
# does not change that -- the task holds the callback, not the reverse. A bare
# create_task() whose handle goes out of scope can therefore be garbage
# collected mid-execution. For the task that drives the entire event that is a
# low-probability, total-blast-radius failure, so it is held explicitly.
_background_tasks: set = set()

# The in-flight game driver, so /admin/abort has something to cancel.
_game_driver_task: asyncio.Task | None = None


def _track(task: asyncio.Task) -> asyncio.Task:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


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

    # Refuse rather than silently "complete" the game from a stale checkpoint.
    # Checked here, at the moment a human is pressing the button, because this
    # is the only point where the answer can still change anything -- once
    # start_games() is running, its `finally` broadcasts game over no matter
    # what happened inside.
    ok, problem = checkpoint_status()
    if not ok:
        logger.error("Refusing to start the game:\n%s", problem)
        raise HTTPException(status_code=409, detail=problem)

    game.game_state = GameState.COUNTDOWN
    game.countdown_end_time = time.time() + COUNTDOWN_SECONDS
    logger.info("Admin %s started the game.", admin_id)

    # The countdown used to be awaited inline, so this endpoint blocked for
    # five seconds before responding -- long enough for an admin dashboard
    # with a short client timeout to report a failure for a game that did in
    # fact start.
    global _game_driver_task
    task = _track(asyncio.create_task(
        _run_countdown_then_start(
            game,
            time_per_round=TIME_PER_ROUND,
            timeout=TURN_TIMEOUT,
            penalty=ROUND_PENALTY,
        )
    ))
    task.add_done_callback(_log_task_exception)
    _game_driver_task = task

    return {"status": "success", "message": "Game countdown initiated successfully."}


@app.post("/admin/abort")
async def abort_game(
    admin_id: int = Depends(verify_admin_session),
    game: GameServer = Depends(get_game_server),
):
    """End the event now, keeping the scores collected so far.

    The escape hatch for the state this had no exit from. game_state is set to
    COUNTDOWN *before* the driver task is created, so if that task dies, or a
    round hangs, the game is pinned in COUNTDOWN/GAME_RUNNING and /admin/rungame
    refuses to do anything ("already started"). The only recovery was a process
    restart -- which then needs all 700 players to log in again before the
    membership snapshot will include their teams.

    This turns "the event is stuck and I have 700 people watching" into "the
    event ended early with real scores on real screens".
    """
    global _game_driver_task

    if game.game_state in (GameState.LOGIN_PERIOD, GameState.GAME_OVER):
        raise HTTPException(
            status_code=400,
            detail=f"No game in progress to abort (state={game.game_state}).",
        )

    logger.warning("Admin %s ABORTED the game from state %s.", admin_id, game.game_state)

    task = _game_driver_task
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Game driver raised while being aborted; continuing to finish.")
    _game_driver_task = None

    # _finish_game is idempotent, so it does not matter whether the driver's
    # own `finally` managed to run it before cancellation took effect.
    await _finish_game(game)

    return {
        "status": "success",
        "message": "Game aborted; final scores broadcast to all connected teams.",
    }


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
