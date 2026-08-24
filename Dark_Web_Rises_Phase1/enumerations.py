"""Shared wire-protocol constants used by both the FastAPI backend and the
JS/TS clients (static/new_client and frontend-fusion). Keep these values in
sync with static/new_client/game_client.js and
frontend-fusion/src/lib/dwr-protocol.ts.
"""
from enum import Enum, IntEnum

try:
    # StrEnum was added in Python 3.11. Azure App Service / Krutrim Cloud /
    # local servers may still ship 3.9 or 3.10, and importing this module
    # would previously raise ImportError before the app could even start.
    from enum import StrEnum
except ImportError:  # pragma: no cover - exercised only on Python < 3.11
    class StrEnum(str, Enum):
        """Minimal backport of enum.StrEnum for Python < 3.11."""

        def __str__(self) -> str:
            return str(self.value)


class JSONFields(StrEnum):
    TYPE = "type"
    STATUS = "status"
    USERNAME = "username"
    PASSWORD = "password"
    AUTHORISED = "authorised"
    TEAM_NAME = "team_name"
    USER_ID = "user_id"
    TEAM_ID = "team_id"
    CONNECTED_TEAM_MEMBERS = "connected_team_members"
    GAME_STATE = "game_state"
    TEAM_STATE = "team_state"
    IMAGE = "image"
    MESSAGE = "message"
    PROMPT = "prompt"
    TIME = "time"
    IS_PLAYER_TURN = "is_player_turn"
    PROMPT_STATUS = "prompt_status"
    ROUND = 'round'
    ROUND_SCORE = 'round_score'
    TEAM_SCORE = "team_score"
    TEAM_RANK = "team_rank"
    TOP3 = "top3"
    ADMIN = "admin"
    ADMIN_ID = "admin_id"
    ADMIN_TOKEN = "admin_token"
    ATTEMPTS_LEFT = "attempts_left"
    # Per-round breakdown, sent alongside the numeric TEAM_SCORE total.
    # Previously GAME_OVER put the *list* of round scores in TEAM_SCORE while
    # ROUND_OVER put an *integer* there, so clients had to type-sniff the
    # same field. TEAM_SCORE is now always a number; the list lives here.
    ROUND_SCORES = "round_scores"
    TOTAL_ROUNDS = "total_rounds"
    # Full ranked leaderboard: [{team_id, team_name, score, rank}, ...]
    LEADERBOARD = "leaderboard"
    # Seconds remaining on a login lockout (see Login.LOCKED).
    RETRY_AFTER = "retry_after"
    # Phase 2 handoff, sent with GAME_OVER. QUALIFIED is per team (top 50% of
    # the final standings); PHASE2_URL is the CTFd entry point and is only
    # populated for teams that qualified.
    QUALIFIED = "qualified"
    PHASE2_URL = "phase2_url"
    # The team's generated phase 2 login, matching its row in the CTFd import
    # CSV. Only ever sent to that team.
    PHASE2_PASSWORD = "phase2_password"
    PHASE2_USERNAME = "phase2_username"


class Login(StrEnum):
    LOGIN = "login"
    DENIED = "denied"
    ACCEPTED = "accepted"
    LOGOUT = "logout"
    # Credentials were not even checked because the account is temporarily
    # locked after repeated failures. Distinct from DENIED so the UI can say
    # "locked, try again in Ns" instead of "wrong password".
    LOCKED = "locked"
    # The password was correct, but this account is already signed in on a
    # device that is still answering. One account, one device -- to move, log
    # out on the first one.
    #
    # Distinct from DENIED because the fix is completely different, and a
    # player told "invalid credentials" here will retype their password until
    # the brute-force lockout triggers. Which is the worst possible outcome:
    # now they cannot log in anywhere for a minute, for doing nothing wrong.
    ALREADY_CONNECTED = "already_connected"
    # Answer to Responses.SESSION_PROBE. Sent automatically by the client; the
    # player never sees it. Its only job is to prove the socket has a live
    # browser behind it and not just an open TCP connection.
    SESSION_PROBE_ACK = "session_probe_ack"


class NameStatus(IntEnum):
    AVAILABLE = 0
    TAKEN = 1
    DNE = -1
    LOCKED = 2
    # The account exists and the password was right, but somebody is already
    # signed in on it from a device that is still responding.
    ALREADY_CONNECTED = 3

class Responses(StrEnum):
    LOGIN_RESPONSE = "login_response"
    LOGOUT_RESPONSE = "logout_response"
    GAME_STATE_RESPONSE = "game_state_response"
    TEAM_STATE_RESPONSE = "team_state_response"
    GAMEPLAY_RESPONSE = "gameplay_response"
    ADMIN_RESPONSE = "admin_response"
    ERROR_RESPONSE = "error_response"
    # Sent to an existing session when a login attempt arrives for the same
    # account somewhere else. It asks one question: is there still a live
    # browser on this socket?
    #
    # Needed because "the server has a socket registered" is NOT evidence that
    # a device is still there. A phone that walks out of wifi range leaves a
    # socket that looks perfectly healthy from the server's side -- writes to
    # it succeed into a TCP buffer -- for anywhere from tens of seconds to
    # several minutes. Denying a login on that basis alone would lock a player
    # out of their own account for as long as the corpse took to be noticed.
    #
    # The client answers with Login.SESSION_PROBE_ACK. An answer means the
    # incumbent is real and the new login is refused; silence means it is gone
    # and the account is released.
    SESSION_PROBE = "session_probe"
    # Sent to a session that is being retired because the account was claimed
    # elsewhere after this one failed to answer a probe. Rare -- it means the
    # socket was slow rather than dead -- but the alternative is a device that
    # sits there showing a game it will never receive another frame for.
    SESSION_REPLACED = "session_replaced"


class GameState(IntEnum):
    LOGIN_PERIOD = 0
    PREGAME = 1
    GAME_RUNNING = 2
    COUNTDOWN = 3
    GAME_OVER = 4
    ROUND_OVER = 6

class TeamState(IntEnum):
    WAITING = 0
    JOINED = 2
    LEFT = 3
    PLAYING = 4
    DONE = 5

class GamePlay(IntEnum):
    NOT_PROMPTED = 0
    PROMPTED = 1
    RECEIVED = 2
    NOT_RECEIVED = 3
    IMAGE_IN = 4
    PROMPT_OUT = 5

    INVALID_PROMPT = 6
    OUT_OF_CHANCES = 7
