//enums 
const JSONFields = Object.freeze({
    TYPE : "type",
    STATUS : "status",
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
    ACCEPTED : "accepted",
    LOGOUT: "logout"
});

const Responses = Object.freeze({
    LOGIN_RESPONSE : "login_response",
    LOGOUT_RESPONSE : "logout_response"
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

        //ui functions
        this.on_login_success = null;
        this.on_login_failed = null;

        this.on_logout_success = null;
        this.on_logout_failed = null;

        //router
        this.socket.onmessage = (event) => {
            const data = JSON.parse(event.data);

            if (data[JSONFields.TYPE] === Responses.LOGIN_RESPONSE)
            {
                if (data[JSONFields.AUTHORISED] === Login.ACCEPTED)
                {
                    this.user_id = data[JSONFields.USER_ID];
                    if (this.on_login_success) this.on_login_success(this.user_id);
                }
                else if (data[JSONFields.AUTHORISED] === Login.DENIED)
                {
                    if (this.on_login_failed) this.on_login_failed(data[JSONFields.STATUS]);
                }
            }

            else if (data[JSONFields.TYPE] === Responses.LOGOUT_RESPONSE)
            {
                if (data[JSONFields.AUTHORISED] === Login.ACCEPTED)
                {
                    if (this.on_logout_success) this.on_logout_success(this.user_id);
                    this.user_id = null;
                }
                else if (data[JSONFields.AUTHORISED] === Login.DENIED)
                {
                    if (this.on_logout_failed) this.on_logout_failed(data[JSONFields.STATUS]);
                }
            }
        };
    }


    //when logging, in call this
    login(username, password) {
        this.socket.send(JSON.stringify({
        [JSONFields.TYPE]: Login.LOGIN,
        [JSONFields.USERNAME]: username,
        [JSONFields.PASSWORD]: password
        }));
    }

    logout(){
        this.socket.send(JSON.stringify({
            [JSONFields.TYPE]: Login.LOGOUT
        }));
    }
    
}

export { GameClient, Login, JSONFields, Responses, NameStatus };