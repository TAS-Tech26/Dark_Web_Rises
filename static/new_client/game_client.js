// --- Global Payload Constants Struct Mapping ---
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
    PROMPT_STATUS : "prompt_status",
    ROUND : "round",
    ROUND_SCORE : "round_score",
    TEAM_SCORE : "team_score",
    TEAM_RANK : "team_rank",
    TOP3 : "top3"
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
    COUNTDOWN : 3,
    GAME_OVER : 4,
    ROUND_OVER : 6
});

const TeamState = Object.freeze({
    WAITING : 0,
    JOINED : 2,
    LEFT : 3,
    PLAYING : 4,
    DONE : 5
});

const GamePlay = Object.freeze({
    NOT_PROMPTED: 0,
    PROMPTED: 1,
    RECEIVED: 2,
    NOT_RECEIVED: 3,
    IMAGE_IN: 4,
    PROMPT_OUT: 5,
    INVALID_PROMPT: 6, 
    OUT_OF_CHANCES: 7   
});

class GameClient {
    constructor(server_url) {   
        this.socket = new WebSocket(server_url);
        this.user_id = null;

        // Custom External Health Check Framework Hooks
        this.on_connected = null;
        this.on_disconnected = null;
        this.on_connection_error = null;

        // Visual Interfacing Updates Handlers Mapping
        this.on_login_success = null;
        this.on_login_failed = null;
        this.on_logout_success = null;
        this.on_logout_failed = null;
        this.on_roster_update = null;
        this.on_game_start = null;
        this.run_turn = null;
        this.on_prompt_success = null;
        this.on_prompt_fail = null;
        this.on_team_done = null;
        this.on_game_countdown = null;
        this.on_round_over = null;
        this.on_game_over = null;

        // Core Network Pipeline Lifecycle Events
        this.socket.onopen = () => {
            if (this.on_connected) this.on_connected();
        };

        this.socket.onclose = () => {
            if (this.on_disconnected) this.on_disconnected();
        };

        this.socket.onerror = (err) => {
            if (this.on_connection_error) this.on_connection_error(err);
        };

        this.socket.onmessage = (event) => { 
            const data = JSON.parse(event.data);

            if (data[JSONFields.TYPE] === Responses.LOGIN_RESPONSE) {
                if (data[JSONFields.AUTHORISED] === Login.ACCEPTED) {
                    this.user_id = data[JSONFields.USER_ID];
                    if (this.on_login_success) this.on_login_success(this.user_id);
                    
                    if (data[JSONFields.GAME_STATE] === GameState.COUNTDOWN) {
                        if (this.on_game_countdown) this.on_game_countdown(data[JSONFields.TIME]);
                    }

                    if (data[JSONFields.TEAM_STATE] === TeamState.PLAYING) {
                        if (data[JSONFields.IS_PLAYER_TURN] === true) {
                            if (this.run_turn) this.run_turn(data[JSONFields.IMAGE], data[JSONFields.TIME]);
                        } else {
                            if (this.on_game_start) this.on_game_start();
                            const gi = document.getElementById("game_image");
                            if (gi && data[JSONFields.IMAGE]) {
                                gi.src = data[JSONFields.IMAGE];
                                gi.style.display = "block";
                            }
                        }
                    } else if (data[JSONFields.TEAM_STATE] === TeamState.DONE) {
                        if (data[JSONFields.GAME_STATE] === GameState.GAME_RUNNING) {
                            if (this.on_team_done) this.on_team_done();
                        } else if (data[JSONFields.GAME_STATE] === GameState.GAME_OVER) {
                            if (this.on_game_over) this.on_game_over(data[JSONFields.TEAM_SCORE], data[JSONFields.TEAM_RANK], data[JSONFields.TOP3]);
                        }
                    }
                } else if (data[JSONFields.AUTHORISED] === Login.DENIED) {
                    if (this.on_login_failed) this.on_login_failed(data[JSONFields.STATUS]);
                }
            }

            else if (data[JSONFields.TYPE] === Responses.LOGOUT_RESPONSE) {
                if (data[JSONFields.AUTHORISED] === Login.ACCEPTED) {
                    if (this.on_logout_success) this.on_logout_success(this.user_id);
                    this.user_id = null;
                } else if (data[JSONFields.AUTHORISED] === Login.DENIED) {
                    if (this.on_logout_failed) this.on_logout_failed(data[JSONFields.STATUS]);
                }
            }

            else if (data[JSONFields.TYPE] === Responses.GAME_STATE_RESPONSE) {
                if (data[JSONFields.GAME_STATE] === GameState.COUNTDOWN) {
                    if (this.on_game_countdown) this.on_game_countdown(data[JSONFields.TIME]);
                } else if (data[JSONFields.GAME_STATE] === GameState.GAME_RUNNING) {
                    if (this.on_game_start) this.on_game_start();
                } else if (data[JSONFields.GAME_STATE] === GameState.ROUND_OVER) {
                    if (this.on_round_over) this.on_round_over(
                        data[JSONFields.ROUND],
                        data[JSONFields.ROUND_SCORE],
                        data[JSONFields.TEAM_SCORE],
                        data[JSONFields.TIME]
                    );
                } else if (data[JSONFields.GAME_STATE] === GameState.GAME_OVER) {
                    if (this.on_game_over) this.on_game_over(data[JSONFields.TEAM_SCORE], data[JSONFields.TEAM_RANK], data[JSONFields.TOP3]);
                }
            }

            else if (data[JSONFields.TYPE] === Responses.TEAM_STATE_RESPONSE) {
                if (data[JSONFields.TEAM_STATE] === TeamState.JOINED || data[JSONFields.TEAM_STATE] === TeamState.LEFT) {
                    if (this.on_roster_update) this.on_roster_update(data[JSONFields.USERNAME]);
                } else if (data[JSONFields.TEAM_STATE] === TeamState.DONE) {
                    if (this.on_team_done) this.on_team_done();
                }
            }

            else if (data[JSONFields.TYPE] === Responses.GAMEPLAY_RESPONSE) {
                if (data[JSONFields.MESSAGE] === GamePlay.IMAGE_IN && data[JSONFields.IS_PLAYER_TURN] === true) {
                    if (this.run_turn) this.run_turn(data[JSONFields.IMAGE], data[JSONFields.TIME]);
                } else if (
                    data[JSONFields.MESSAGE] === GamePlay.RECEIVED || 
                    data[JSONFields.MESSAGE] === GamePlay.INVALID_PROMPT || 
                    data[JSONFields.MESSAGE] === GamePlay.OUT_OF_CHANCES
                ) {
                    if (this.on_prompt_success) {
                        this.on_prompt_success(
                            data[JSONFields.PROMPT_STATUS], 
                            data[JSONFields.MESSAGE],
                            data["attempts_left"]           
                        );
                    }
                } else if (data[JSONFields.MESSAGE] === GamePlay.NOT_RECEIVED) {
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

    logout() {
        this.socket.send(JSON.stringify({
            [JSONFields.TYPE]: Login.LOGOUT
        }));
    }

    send_prompt(prompt_status, prompt) {
        this.socket.send(JSON.stringify({
            [JSONFields.TYPE]: GamePlay.PROMPT_OUT,
            [JSONFields.STATUS]: prompt_status,
            [JSONFields.PROMPT]: prompt
        }));
    }
}

export { GameClient, Login, JSONFields, Responses, NameStatus, GamePlay };