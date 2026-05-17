const JSONFields = Object.freeze({
    TYPE : "type",
    STATUS : "status",
    ID : "ID",
    USERNAME : "username",
    PASSWORD : "password",
    AUTHORISED : "authorised",
    TEAM_NAME : "team_name",
    USER_ID : "user_id",
    TEAM_ID : "team_id"
});

const Login = Object.freeze({
    LOGIN : "login",
    DENIED : "denied",
    ACCEPTED : "accepted"
});

const Responses = Object.freeze({
    LOGIN_RESPONSE : "login_response"
});

const NameStatus = Object.freeze({
    AVAILABLE : 0,
    TAKEN : 1,
    DNE : -1
});


class GameClient
{
    constructor(server_url)
    {
        this.socket = new WebSocket(server_url);
        this.user_id = null;

        this.on_login_success = null;
        this.on_login_failed = null;

        this.socket.onmessage = (event) => {
            const data = JSON.parse(event.data);

            if (data[JSONFields.TYPE] === Responses.LOGIN_RESPONSE)
            {
                if (data[JSONFields.AUTHORISED] === Login.ACCEPTED)
                {
                    this.user_id = data[JSONFields.ID];
                    if (this.on_login_success) this.on_login_success(this.user_id);
                }
                else if (data[JSONFields.AUTHORISED] === Login.DENIED)
                {
                    if (this.on_login_failed) this.on_login_failed(data[JSONFields.STATUS]);
                }
            }
        };
    }

    login(username, password) {
    this.socket.send(JSON.stringify({
      [JSONFields.TYPE]: Login.LOGIN,
      [JSONFields.USERNAME]: username,
      [JSONFields.PASSWORD]: password
    }));
    }
}

export { GameClient, Login, JSONFields, Responses, NameStatus };