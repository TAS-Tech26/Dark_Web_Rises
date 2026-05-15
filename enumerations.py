from enum import StrEnum, IntEnum

class JSONFields(StrEnum):
    TYPE = "type"
    STATUS = "status"
    ID = "ID"
    USERNAME = "username"
    TEAM_NAME = "team_name"
    USER_ID = "user_id"
    TEAM_ID = "team_id"

class Login(StrEnum):
    LOGIN = "login"
    LOGIN_RESPONSE = "login_resposne"
    SELECT_TEAM_RESPONSE = "select_team_response"
    SELECT_TEAM = "select_team"
    JOIN_TEAM = "join_team"
    CHANGE_TEAM_NAME = "change_team_name"

class NameStatus(IntEnum):
    AVAILABLE = 0
    TAKEN = 1
    DNE = -1