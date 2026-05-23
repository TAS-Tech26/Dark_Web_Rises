//enums 
const JSONFields = Object.freeze({
    TYPE : "type",
    STATUS : "status",
    USERNAME : "username",
    PASSWORD : "password",
    AUTHORISED : "authorised",
    TEAM_NAME : "team_name",
    USER_ID : "user_id",
    TEAM_ID : "team_id",
    CONNECTED_TEAM_MEMBERS : "connected_team_members",
    GAME_STATE : "game_state",
    TEAM_STATE : "team_state",
    IMAGE : "image",
    MESSAGE : "message",
    PROMPT : "prompt",
    TIME : "time",
    IS_PLAYER_TURN : "is_player_turn",
    PROMPT_STATUS : "prompt_status"
});

const Login = Object.freeze({
    LOGIN : "login",
    DENIED : "denied",
    ACCEPTED : "accepted",
    LOGOUT: "logout"
});

const Responses = Object.freeze({
    LOGIN_RESPONSE : "login_response",
    LOGOUT_RESPONSE : "logout_response",
    GAME_STATE_RESPONSE : "game_state_response",
    TEAM_STATE_RESPONSE : "team_state_response",
    GAMEPLAY_RESPONSE : "gameplay_response"
});

const NameStatus = Object.freeze({
    AVAILABLE : 0,
    TAKEN : 1,
    DNE : -1
});

const GameState = Object.freeze({
    LOGIN_PERIOD : 0,
    PREGAME : 1,
    GAME_RUNNING : 2,
    COUNTDOWN : 3
});

const TeamState = Object.freeze({
    WAITING : 0,
    JOINED : 2,
    LEFT : 3,
    PLAYING : 4,
    DONE : 5
});

const GamePlay = Object.freeze({
    NOT_PROMPTED : 0,
    PROMPTED : 1,
    RECEIVED : 2,
    NOT_RECEIVED : 3,
    IMAGE_IN : 4,
    PROMPT_OUT : 5
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

        this.on_team_ready = null;
        this.on_team_unready = null;

        this.on_roster_update = null;

        this.on_game_start = null;

        this.run_turn = null;

        this.on_prompt_success = null;
        this.on_prompt_fail = null;

        this.on_game_countdown = null;

        //router
        this.socket.onmessage = (event) => { 
            const data = JSON.parse(event.data);

            if (data[JSONFields.TYPE] === Responses.LOGIN_RESPONSE)
            {
                if (data[JSONFields.AUTHORISED] === Login.ACCEPTED)
                {
                    if (data[JSONFields.TEAM_STATE] === TeamState.WAITING)
                    {
                        if (data[JSONFields.GAME_STATE] === GameState.LOGIN_PERIOD)
                        {
                            this.user_id = data[JSONFields.USER_ID];
                            if (this.on_login_success) this.on_login_success(this.user_id);
                        }
                        else if (data[JSONFields.GAME_STATE] === GameState.COUNTDOWN)
                        {
                            this.user_id = data[JSONFields.USER_ID]
                            if (this.on_login_success) this.on_login_success(this.user_id);
                            if (this.on_game_countdown) this.on_game_countdown(data[JSONFields.TIME]);
                        }
                    }
                    else if (data[JSONFields.TEAM_STATE] == TeamState.PLAYING)
                    {
                        this.user_id = data[JSONFields.USER_ID];
                        if (this.on_login_success) this.on_login_success(this.user_id);
                        if (this.run_turn) this.run_turn(data[JSONFields.IMAGE], data[JSONFields.TIME]);
                    }
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

            else if (data[JSONFields.TYPE] === Responses.GAME_STATE_RESPONSE)
            {
                if (data[JSONFields.MESSAGE] === GameState.COUNTDOWN)
                {
                    
                    if (this.on_game_countdown) this.on_game_countdown(data[JSONFields.TIME]);
                }
                else if (data[JSONFields.MESSAGE] === GameState.GAME_RUNNING)
                {
                    if (this.on_game_start) this.on_game_start();
                }
            }

            else if (data[JSONFields.TYPE] === Responses.TEAM_STATE_RESPONSE)
            {
                if (data[JSONFields.TEAM_STATE] === TeamState.JOINED || data[JSONFields.TEAM_STATE] === TeamState.LEFT) 
                {
                    if (this.on_roster_update) this.on_roster_update(data[JSONFields.USERNAME]);
                }
            }

            else if (data[JSONFields.TYPE] === Responses.GAMEPLAY_RESPONSE)
            {
                if (data[JSONFields.MESSAGE] === GamePlay.IMAGE_IN && data[JSONFields.IS_PLAYER_TURN] === true)
                {
                    if (this.run_turn) this.run_turn(data[JSONFields.IMAGE], data[JSONFields.TIME]);
                }
                else if (data[JSONFields.MESSAGE] === GamePlay.RECEIVED)
                {
                    if (this.on_prompt_success) this.on_prompt_success(data[JSONFields.PROMPT_STATUS]);
                }
                else if (data[JSONFields.MESSAGE] === GamePlay.NOT_RECEIVED)
                {
                    if (this.on_prompt_fail) this.on_prompt_fail();
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

    logout(){
        this.socket.send(JSON.stringify({
            [JSONFields.TYPE]: Login.LOGOUT
        }));
    }

    send_prompt(prompt_status, prompt){
        this.socket.send(JSON.stringify({
            [JSONFields.TYPE]: GamePlay.PROMPT_OUT,
            [JSONFields.STATUS]: prompt_status,
            [JSONFields.PROMPT]: prompt
        }));
    }
    
}

export { GameClient, Login, JSONFields, Responses, NameStatus };