from fastapi import FastAPI, WebSocket, Depends, WebSocketDisconnect
from GameServer import GameServer, User, Team, get_image, classify_prompt,compare_image
from enumerations import JSONFields, Login, NameStatus, Responses, GameState, TeamState, GamePlay
from fastapi.staticfiles import StaticFiles
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

async def run_game(game: GameServer, team: Team, time_per_round, timeout, penalty):
        round = 0
        no_prompt_penalty = 5
        penalty_multiplier = game.max_members_per_team/len(team.connected_sockets)
        for round in range(5):
            team.current_image = await get_image("default_prompt")
            team.images.append([])
            team.images[round].append(team.current_image)
            team.original_image = team.current_image

            for uid in team.members:
                team.current_turn_uid = uid
                team.turn_end_time = time.time() + time_per_round

                if uid in team.connected_sockets:
                    await team.connected_sockets[uid].send_json({
                        JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                        JSONFields.MESSAGE: GamePlay.IMAGE_IN,
                        JSONFields.IS_PLAYER_TURN: True,
                        JSONFields.IMAGE: team.current_image,
                        JSONFields.TIME: time_per_round
                    })
                while True:
                    try: 
                        time_left = team.turn_end_time - time.time()
                        if time_left > 0:
                            data = await asyncio.wait_for(team.input_queue.get(), timeout=time_left)
                        else:
                            raise asyncio.TimeoutError()
                        #making a set of attempts done by each player in their turn
                        if not hasattr(team, "player_attempts"):
                            team.player_attempts = {}
                        if uid not in team.player_attempts:
                            team.player_attempts[uid] = 0

                        if data.get(JSONFields.STATUS) == GamePlay.PROMPTED:
                            current_prompt = data.get(JSONFields.PROMPT)
                            is_prompt_valid = classify_prompt(prompt=current_prompt)

                            if is_prompt_valid:
                                team.current_image = await get_image(prompt=data.get(JSONFields.PROMPT))
                                team.images[round].append(team.current_image)
                                if uid in team.connected_sockets:
                                    await team.connected_sockets[uid].send_json({
                                        JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                                        JSONFields.PROMPT_STATUS: is_prompt_valid,
                                        JSONFields.MESSAGE: GamePlay.RECEIVED
                                    }) 
                                break
                            else:
                                # 2. INVALID PROMPT SUBMITTED
                                team.player_attempts[uid] += 1  # Increment the individual player's tracker
                                current_attempts = team.player_attempts[uid]
                                
                                if current_attempts >= 3:
                                    # 3. STRIKE THREE -> FORCED TURN END
                                    if uid in team.connected_sockets:
                                        await team.connected_sockets[uid].send_json({
                                            JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                                            JSONFields.PROMPT_STATUS: False,
                                            # Use .value or the Enum integer explicitly so JS knows what to expect
                                            JSONFields.MESSAGE: GamePlay.OUT_OF_CHANCES.value, 
                                            "attempts_left": 0
                                        })
                                    no_prompt_penalty += penalty * penalty_multiplier
                                    team.player_attempts[uid] = 0
                                    break # Break the while loop to skip to the next player
                                
                                else:
                                    # 4. STILL HAS CHANCES LEFT -> TRY AGAIN
                                    if uid in team.connected_sockets:
                                        await team.connected_sockets[uid].send_json({
                                            JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                                            JSONFields.PROMPT_STATUS: False,
                                            JSONFields.MESSAGE: GamePlay.INVALID_PROMPT,#.value was removed
                                            "attempts_left": 3 - current_attempts 
                                        })
                        elif data.get(JSONFields.STATUS) == GamePlay.NOT_PROMPTED:
                            if uid in team.connected_sockets:
                                await team.connected_sockets[uid].send_json({
                                    JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                                    JSONFields.MESSAGE: GamePlay.NOT_RECEIVED
                                })
                                no_prompt_penalty += penalty * penalty_multiplier
                                break
                            
                    except (asyncio.TimeoutError, TimeoutError):
                        no_prompt_penalty += penalty * penalty_multiplier
                        break

            round_score = await compare_image(no_prompt_penalty,team.original_image,team.current_image)
            team.score.append(round_score)
            await team.announce_to_team({
                JSONFields.TYPE: Responses.GAME_STATE_RESPONSE,
                JSONFields.GAME_STATE: GameState.ROUND_OVER,
                JSONFields.ROUND: round + 1,
                JSONFields.ROUND_SCORE: round_score,
                JSONFields.TEAM_SCORE: sum(team.score),
                JSONFields.TIME: 10
            })
            await asyncio.sleep(10)

        team.team_state = TeamState.DONE

        await team.announce_to_team({
            JSONFields.TYPE: Responses.TEAM_STATE_RESPONSE,
            JSONFields.TEAM_STATE: TeamState.DONE
        })

        game.scores[team.id] = sum(team.score)

async def start_games(game:GameServer, time_per_round, timeout, penalty):
        tasks = []
        
        for team in game.teams:
            if len(team.connected_sockets) > 0:
                team.team_state = TeamState.PLAYING
                task = asyncio.create_task(run_game(game,team, time_per_round, timeout, penalty))
                tasks.append(task)

            else:
                team.team_state = TeamState.DONE
                team.score = -1 
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

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

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

