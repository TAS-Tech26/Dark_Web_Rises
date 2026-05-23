from fastapi import FastAPI, WebSocket, Depends, WebSocketDisconnect
from GameServer import GameServer, User, Team
from enumerations import JSONFields, Login, NameStatus, Responses, GameState, TeamState, GamePlay
import json
import asyncio
import time

server = GameServer(server_id=0,
                    max_teams=3,
                    max_members_per_team=4,
                    team_names=["name1", "name2", "name3"],
                    user_data={ 
                        0:["user1", "password1", 0],
                        1:["user2", "password2", 0],
                        2:["user3", "password3", 2],
                        3:["user4", "password4", 0],
                        4:["user5", "password5", 0]
                    })

def get_game_server():
    return server

app = FastAPI()

@app.websocket("/ws")
async def game_endpoint(websocket: WebSocket, game: GameServer = Depends(get_game_server)):
    await websocket.accept()
    current_user_id = None
    current_team_id = None
    try:
        while True:
            data = await websocket.receive_json() 

            """ 
                data format:
                TYPE: <some type (LOGIN/GAMEPLAY etc)>
                <required fields under that type>
            """

            #login check
            if data.get(JSONFields.TYPE) == Login.LOGIN:
                response = game.check_login(data, websocket)

                if response.get(JSONFields.AUTHORISED) == Login.ACCEPTED:
                    current_user_id = response.get(JSONFields.USER_ID)
                    current_team_id = game.users[current_user_id].team_id
                        
                    await websocket.send_json(response)

                    await game.teams[current_team_id].announce_to_team({JSONFields.TYPE: Responses.TEAM_STATE_RESPONSE,
                                                                  JSONFields.TEAM_STATE: TeamState.JOINED,
                                                                  JSONFields.USERNAME: [game.users[uid].username for uid in game.connected_teams[current_team_id]]})
                    
                else:
                    await websocket.send_json(response)

            elif current_user_id != None:
                #logout check
                if data.get(JSONFields.TYPE) == Login.LOGOUT:
                    response = game.check_logout(user_id=current_user_id)

                    await websocket.send_json(response)

                    await game.teams[current_team_id].announce_to_team({JSONFields.TYPE: Responses.TEAM_STATE_RESPONSE,
                                                                  JSONFields.TEAM_STATE: TeamState.LEFT,
                                                                  JSONFields.USERNAME: [game.users[uid].username for uid in game.connected_teams[current_team_id]]})

                    current_user_id = None
                    current_team_id = None

                if game.game_state == GameState.GAME_RUNNING or game.game_state == GameState.COUNTDOWN:
                    if data.get(JSONFields.TYPE) == GamePlay.PROMPT_OUT:
                        if current_user_id == game.teams[current_team_id].current_turn_uid:
                            await game.teams[current_team_id].input_queue.put(data)

            else:
                await websocket.send_json({JSONFields.TYPE: None})
            
    except WebSocketDisconnect:
        if current_user_id is not None:
            if current_user_id in game.connected_users:
                game.connected_users.remove(current_user_id)
                game.connected_sockets.pop(current_user_id)

            if current_user_id in game.connected_teams[current_team_id]:
                game.connected_teams[current_team_id].remove(current_user_id)
                game.teams[current_team_id].connected_sockets.pop(current_user_id)

            await game.teams[current_team_id].announce_to_team({JSONFields.TYPE: Responses.TEAM_STATE_RESPONSE,
                                                          JSONFields.TEAM_STATE: TeamState.LEFT,
                                                          JSONFields.USERNAME: [game.users[uid].username for uid in game.connected_teams[current_team_id]]})

            current_user_id = None
            current_team_id = None

            
        print("client disconnected")

@app.post("/admin/rungame")
async def force_start_game(game: GameServer = Depends(get_game_server)):
    game.game_state = GameState.COUNTDOWN 

    game.countdown_end_time = time.time() + 5

    await game.announce_to_users({
        JSONFields.TYPE: Responses.GAME_STATE_RESPONSE,
        JSONFields.MESSAGE: GameState.COUNTDOWN,
        JSONFields.TIME: 5 
    })

    await asyncio.sleep(5)

    game.game_state = GameState.GAME_RUNNING

    await game.announce_to_users({
        JSONFields.TYPE: Responses.GAME_STATE_RESPONSE,
        JSONFields.MESSAGE: GameState.GAME_RUNNING
    })

    await game.start_games(time_per_round=30, timeout=30, penalty=5)

    return {"message": "game running"}

