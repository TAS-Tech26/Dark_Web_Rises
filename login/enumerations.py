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
    

class GameState(IntEnum):
    LOGIN_PERIOD = 0
    PREGAME = 1
    GAME_RUNNING = 2

class TeamState(IntEnum):
    NOT_READY = 0
    READY = 1
    JOINED = 2
    LEFT = 3
