from enum import StrEnum, IntEnum

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
    TEAM_SCORE = "team_score"
    TEAM_RANK = "team_rank"
    TOP3 = "top3"

class Login(StrEnum):
    LOGIN = "login"
    DENIED = "denied"
    ACCEPTED = "accepted"
    LOGOUT = "logout"

class NameStatus(IntEnum):
    AVAILABLE = 0
    TAKEN = 1
    DNE = -1

class Responses(StrEnum):
    LOGIN_RESPONSE = "login_response"
    LOGOUT_RESPONSE = "logout_response"
    GAME_STATE_RESPONSE = "game_state_response"
    TEAM_STATE_RESPONSE = "team_state_response"
    GAMEPLAY_RESPONSE = "gameplay_response"
    

class GameState(IntEnum):
    LOGIN_PERIOD = 0
    PREGAME = 1
    GAME_RUNNING = 2
    COUNTDOWN = 3
    GAME_OVER = 4

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


