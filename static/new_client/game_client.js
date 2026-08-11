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
    TOP3 : "top3",
    ADMIN_ID : "admin_id",
    ADMIN_TOKEN : "admin_token",
    ATTEMPTS_LEFT : "attempts_left",
    ROUND_SCORES : "round_scores",
    TOTAL_ROUNDS : "total_rounds",
    LEADERBOARD : "leaderboard",
    RETRY_AFTER : "retry_after"
});

const Login = Object.freeze({
    LOGIN : "login",
    DENIED : "denied",
    ACCEPTED : "accepted",
    LOGOUT: "logout",
    LOCKED : "locked"
});

const Responses = Object.freeze({
    LOGIN_RESPONSE : "login_response",
    LOGOUT_RESPONSE : "logout_response",
    GAME_STATE_RESPONSE : "game_state_response",
    TEAM_STATE_RESPONSE : "team_state_response",
    GAMEPLAY_RESPONSE : "gameplay_response",
    ADMIN_RESPONSE : "admin_response",
    // The backend has always been able to send this (unauthenticated
    // request, malformed frame, rate limited, not your turn); neither client
    // knew the constant, so those frames were silently dropped and the user
    // just saw nothing happen.
    ERROR_RESPONSE : "error_response"
});

const NameStatus = Object.freeze({
    AVAILABLE : 0,
    TAKEN : 1,
    DNE : -1,
    LOCKED : 2
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
        // Opaque per-session token required by the /admin/* HTTP endpoints.
        // Only set for admin sessions (see on_admin_login_success below).
        this.admin_token = null;

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
        this.on_admin_login_success = null;
        this.on_server_error = null;
        this.on_login_locked = null;

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
            console.log("RAW INCOMING WEBSOCKET PACKET:", event.data);
            let data;
            try {
                data = JSON.parse(event.data);
            } catch (err) {
                console.error("Received malformed (non-JSON) packet from server:", err);
                return;
            }
            if (data[JSONFields.TYPE] === Responses.ERROR_RESPONSE) {
                const message = data[JSONFields.MESSAGE];
                console.warn("Server error frame:", message);
                if (this.on_server_error) this.on_server_error(message);
                return;
            }
            if (data[JSONFields.TYPE] === Responses.ADMIN_RESPONSE) {
                if (data[JSONFields.AUTHORISED] === Login.ACCEPTED) {
                    // Use ?? (nullish coalescing) instead of || so an admin id of 0 is preserved
                    this.user_id = data[JSONFields.ADMIN_ID] ?? data[JSONFields.USER_ID];
                    this.admin_token = data[JSONFields.ADMIN_TOKEN] ?? null;

                    console.log("Admin parsed session key ID:", this.user_id);

                    if (!this.admin_token) {
                        console.error("Admin login accepted but no admin_token was returned by the server.");
                    }

                    if (this.on_admin_login_success) this.on_admin_login_success(this.user_id, this.admin_token);
                } else {
                    if (this.on_login_failed) this.on_login_failed(data[JSONFields.STATUS]);
                }
                return; // Halt cascade processing
            }
            if (data[JSONFields.TYPE] === Responses.LOGIN_RESPONSE) {
                if (data[JSONFields.AUTHORISED] === Login.ACCEPTED) {
                    this.user_id = data[JSONFields.USER_ID];
                    if (this.on_login_success) this.on_login_success(this.user_id);

                    if (data[JSONFields.GAME_STATE] === GameState.COUNTDOWN) {
                        if (this.on_game_countdown) this.on_game_countdown(data[JSONFields.TIME]);
                    }

                    if (data[JSONFields.TEAM_STATE] === TeamState.PLAYING) {
                        // Pass the image, time limit, and the turn status directly to run_turn
                        if (this.run_turn) {
                            this.run_turn(
                                data[JSONFields.IMAGE],
                                data[JSONFields.TIME],
                                data[JSONFields.IS_PLAYER_TURN]
                            );
                        }
                    } else if (data[JSONFields.TEAM_STATE] === TeamState.DONE) {
                        if (data[JSONFields.GAME_STATE] === GameState.GAME_RUNNING) {
                            if (this.on_team_done) this.on_team_done();
                        } else if (data[JSONFields.GAME_STATE] === GameState.GAME_OVER) {
                            if (this.on_game_over) this.on_game_over(data[JSONFields.TEAM_SCORE], data[JSONFields.TEAM_RANK], data[JSONFields.TOP3]);
                        }
                    }
                } else if (data[JSONFields.AUTHORISED] === Login.LOCKED) {
                    // Distinct from DENIED: the password was never checked
                    // because the account is temporarily locked out.
                    if (this.on_login_locked) this.on_login_locked(data[JSONFields.RETRY_AFTER]);
                    else if (this.on_login_failed) this.on_login_failed(data[JSONFields.STATUS]);
                } else if (data[JSONFields.AUTHORISED] === Login.DENIED) {
                    if (this.on_login_failed) this.on_login_failed(data[JSONFields.STATUS]);
                }
            }

            else if (data[JSONFields.TYPE] === Responses.LOGOUT_RESPONSE) {
                if (data[JSONFields.AUTHORISED] === Login.ACCEPTED) {
                    if (this.on_logout_success) this.on_logout_success(this.user_id);
                    this.user_id = null;
                    this.admin_token = null;
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

            else if (data[JSONFields.TYPE] === Responses.GAMEPLAY_RESPONSE) {
                const message = data[JSONFields.MESSAGE];
                if (message === GamePlay.IMAGE_IN) {
                    if (this.run_turn) {
                        this.run_turn(
                            data[JSONFields.IMAGE],
                            data[JSONFields.TIME],
                            data[JSONFields.IS_PLAYER_TURN]
                        );
                    }
                } else if (message === GamePlay.RECEIVED) {
                    // Bug fix: this branch previously didn't exist at all, so a
                    // successfully-submitted prompt never told the UI to stop
                    // showing "Transmitting parameters..." -- the active
                    // player's screen looked frozen until the next turn image
                    // arrived.
                    if (this.on_prompt_success) this.on_prompt_success(true, GamePlay.RECEIVED, null);
                } else if (message === GamePlay.INVALID_PROMPT) {
                    if (this.on_prompt_success) this.on_prompt_success(false, GamePlay.INVALID_PROMPT, data[JSONFields.ATTEMPTS_LEFT]);
                } else if (message === GamePlay.OUT_OF_CHANCES) {
                    if (this.on_prompt_success) this.on_prompt_success(false, GamePlay.OUT_OF_CHANCES, 0);
                } else if (message === GamePlay.NOT_RECEIVED) {
                    if (this.on_prompt_fail) this.on_prompt_fail();
                }
            }
            else if (data[JSONFields.TYPE] === Responses.TEAM_STATE_RESPONSE) {
                if (data[JSONFields.TEAM_STATE] === TeamState.JOINED || data[JSONFields.TEAM_STATE] === TeamState.LEFT) {
                    if (this.on_roster_update) this.on_roster_update(data[JSONFields.USERNAME]);
                } else if (data[JSONFields.TEAM_STATE] === TeamState.DONE) {
                    if (this.on_team_done) this.on_team_done();
                }
            }

        };
    }

    // Admins authenticate through the same login() call as players -- the
    // backend identifies the account type server-side and replies with
    // an admin_response (handled above) instead of a login_response.
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
