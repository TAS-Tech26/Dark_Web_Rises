
from enumerations import JSONFields, Login, NameStatus, Responses
import json

"""Login format:
    TYPE: LOGIN
    USERNAME: <name>
    PASSWORD: <password>
"""

def check_login(data, users):
    response = {
                JSONFields.TYPE: Responses.LOGIN_RESPONSE,
                JSONFields.STATUS: NameStatus.DNE,
                JSONFields.AUTHORISED: Login.DENIED,
                JSONFields.ID: None
                }

    username = data.get(JSONFields.USERNAME)
    pwd = data.get(JSONFields.PASSWORD)

    for user in users:
        if username == user.username:
            response[JSONFields.STATUS] = NameStatus.AVAILABLE
            if user.password == pwd:
                response[JSONFields.AUTHORISED] = Login.ACCEPTED
                response[JSONFields.ID] = user.id

            break
        
    return response
