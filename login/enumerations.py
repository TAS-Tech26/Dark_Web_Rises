from enum import StrEnum, IntEnum

class JSONFields(StrEnum):
    TYPE = "type"
    STATUS = "status"
    ID = "ID"
    USERNAME = "username"
    PASSWORD = "password"
    AUTHORISED = "authorised"
    TEAM_NAME = "team_name"
    USER_ID = "user_id"
    TEAM_ID = "team_id"

class Login(StrEnum):
    LOGIN = "login"
    DENIED = "denied"
    ACCEPTED = "accepted"

class NameStatus(IntEnum):
    AVAILABLE = 0
    TAKEN = 1
    DNE = -1

class Responses(StrEnum):
    LOGIN_RESPONSE = "login_response"
