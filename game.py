import time
import asyncio
from fastapi import FastAPI, WebSocket, Depends, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from enumerations import JSONFields, Login, Responses, GameState, TeamState, GamePlay
from models.server import GameServer

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


app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

def get_game_server():
    return server

async def start_games(game:GameServer, time_per_round, timeout, penalty):
        tasks = []
        
        for team in game.teams:
            if len(team.connected_sockets) > 0:
                team.team_state = TeamState.PLAYING
                task = asyncio.create_task(team.run_game(game.scores,game.max_members_per_team, time_per_round,timeout, penalty))
                tasks.append(task)

            else:
                team.team_state = TeamState.DONE
                team.score =[-1]
                game.scores[team.id] = -1

        if tasks: 
            await asyncio.gather(*tasks)

            print("All teams finished the game")        

        game.game_state = GameState.GAME_OVER

        
        top3 = game.rank_teams()

        for team in game.teams:
            await team.announce_to_team({
                JSONFields.TYPE: Responses.GAME_STATE_RESPONSE,
                JSONFields.GAME_STATE: GameState.GAME_OVER,
                JSONFields.TEAM_SCORE: team.score,
                JSONFields.TEAM_RANK: team.rank,
                JSONFields.TOP3: top3
            })


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

                elif game.game_state == GameState.GAME_RUNNING or game.game_state == GameState.COUNTDOWN:
                    if data.get(JSONFields.TYPE) == GamePlay.PROMPT_OUT:
                        if current_user_id == game.teams[current_team_id].current_turn_uid:
                            await game.teams[current_team_id].input_queue.put(data)
            else:
                await websocket.send_json({JSONFields.TYPE: None})
            
    except WebSocketDisconnect:
        if current_user_id is not None:
            if current_user_id in game.connected_users:
                game.connected_users.remove(current_user_id)
                game.connected_sockets.pop(current_user_id,None)

            if current_user_id in game.connected_teams[current_team_id]:
                game.connected_teams[current_team_id].remove(current_user_id)
                game.teams[current_team_id].connected_sockets.pop(current_user_id,None)

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
        JSONFields.GAME_STATE: GameState.COUNTDOWN,
        JSONFields.TIME: 5 
    })

    await asyncio.sleep(5)
    game.game_state = GameState.GAME_RUNNING
    await game.announce_to_users({
        JSONFields.TYPE: Responses.GAME_STATE_RESPONSE,
        JSONFields.GAME_STATE: GameState.GAME_RUNNING
    })
    await start_games(game,time_per_round=90, timeout=30, penalty=5)
    return {"message": "game running"}
#changes have been made