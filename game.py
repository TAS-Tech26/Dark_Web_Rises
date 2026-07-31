import time
import asyncio

from fastapi import FastAPI, WebSocket, Depends, WebSocketDisconnect, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from supabase import create_client, Client

from enumerations import JSONFields, Login, Responses, GameState, TeamState, GamePlay
from models.server import GameServer
#from reader import generate_team_dictionary

import json
import os
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "https://id-preview--76b9f65f-7092-4b6e-8af6-df601bbc7578.lovable.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATE_FILE = "game_state.json"
'''
supabase_url: str = os.environ.get("SUPABASE_URL")
supabase_key: str = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(supabase_url, supabase_key)

player_data, team_count = generate_team_dictionary(supabase)
'''

server = GameServer(server_id=0,
                    max_teams=3,
                    max_members_per_team=4,
                    team_names=["name1", "name2", "name3","name4","name5","name6"],
                    player_data={ 
                        0:["user1", "password1", 0],
                        1:["user2", "password2", 0],
                        2:["user3", "password3", 2],
                        3:["user4", "password4", 0],
                        4:["user5", "password5", 0]},
                    admin_data = {
                        0:["admin1","admin_password1"],
                        1:["admin2","admin_password2"]
                    })

app.mount("/static", StaticFiles(directory="static"), name="static")

def save_game_checkpoint(completed_round: int, round_data: dict, inactive_teams: set):
    data = {
        "last_completed_round": completed_round,
        "rounds": {},
        "inactive_teams": list(inactive_teams)
    }

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                existing_data = json.load(f)
                data["rounds"] = existing_data.get("rounds", {})
        except json.JSONDecodeError:
            pass

    data["rounds"][str(completed_round)] = round_data
    data["last_completed_round"] = completed_round

    temp_file = f"{STATE_FILE}.tmp"
    with open(temp_file, "w") as f:
        json.dump(data, f, indent=4)
    os.replace(temp_file, STATE_FILE)


def load_game_checkpoint():
    if not os.path.exists(STATE_FILE):
        return 0, {}

    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)

        last_round = data.get("last_completed_round", -1)
        rounds_history = data.get("rounds", {})
        return last_round + 1, rounds_history
    except (json.JSONDecodeError, KeyError):
        return 0, {}
    
def get_game_server():
    return server

async def start_games(game: GameServer, time_per_round, timeout, penalty): 
    start_round_num, rounds_history = load_game_checkpoint()

    active_teams = []
    inactive_team_ids = set()

    for team in game.teams:
        if len(team.connected_sockets) > 0:
            team.team_state = TeamState.PLAYING
            team.rotation_order = list(team.members)
            active_teams.append(team)

            team.score = []
            for r in range(start_round_num):
                score = rounds_history.get(str(r), {}).get(str(team.id), 0)
                team.score.append(score)
        else:
            team.team_state = TeamState.DONE
            team.score = [-1]
            game.scores[team.id] = -1
            inactive_team_ids.add(team.id)

    if active_teams:
        for round_num in range(start_round_num, 5):
            game.current_round = round_num+1
            round_tasks = [
                asyncio.create_task(team.run_round(round_num, time_per_round, timeout, penalty))
                for team in active_teams
            ]
            round_scores = await asyncio.gather(*round_tasks)
            
            round_data = {str(team.id): score for team, score in zip(active_teams, round_scores)}
            print(f"Round {round_num + 1} complete for all teams: {round_data}")

            save_game_checkpoint(round_num, round_data, inactive_team_ids)

            for team in active_teams:
                await team.announce_to_team({
                    JSONFields.TYPE: Responses.GAME_STATE_RESPONSE,
                    JSONFields.GAME_STATE: GameState.ROUND_OVER,
                    JSONFields.ROUND: round_num + 1,
                    JSONFields.ROUND_SCORE: round_data[str(team.id)],
                    JSONFields.TEAM_SCORE: sum(team.score),
                    JSONFields.TIME: 10
                })
            await asyncio.sleep(10)
            
        print("All teams finished the game")

    for team in active_teams:
        team.team_state = TeamState.DONE
        game.scores[team.id] = sum(team.score)
        await team.announce_to_team({
            JSONFields.TYPE: Responses.TEAM_STATE_RESPONSE,
            JSONFields.TEAM_STATE: TeamState.DONE
        })
        
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
    current_client_type = None # Track whether this is an admin or player connection
    
    try:
        while True:
            data = await websocket.receive_json() 

            # Login routing
            if data.get(JSONFields.TYPE) == Login.LOGIN:
                response = game.check_login(data, websocket)
                
                if response.get(JSONFields.TYPE) == Responses.ADMIN_RESPONSE:
                    if response.get(JSONFields.AUTHORISED) == Login.ACCEPTED:
                        current_user_id = response.get(JSONFields.ADMIN_ID)
                        current_client_type = "admin"
                        await websocket.send_json(response)
                    else:
                        await websocket.send_json(response)
                else:
                    if response.get(JSONFields.AUTHORISED) == Login.ACCEPTED:
                        current_user_id = response.get(JSONFields.USER_ID)
                        current_team_id = game.players[current_user_id].team_id
                        current_client_type = "player"
                            
                        await websocket.send_json(response)

                        await game.teams[current_team_id].announce_to_team({
                            JSONFields.TYPE: Responses.TEAM_STATE_RESPONSE,
                            JSONFields.TEAM_STATE: TeamState.JOINED,
                            JSONFields.USERNAME: [game.players[uid].username for uid in game.connected_teams[current_team_id]]
                        })
                    else:
                        await websocket.send_json(response)

            # Authenticated Session Management Loop
            elif current_user_id is not None:
                if data.get(JSONFields.TYPE) == Login.LOGOUT:
                    response = game.check_logout(id=current_user_id, client_type=current_client_type)
                    await websocket.send_json(response)

                    if current_client_type == "player" and response.get(JSONFields.AUTHORISED) == Login.ACCEPTED:
                        await game.teams[current_team_id].announce_to_team({
                            JSONFields.TYPE: Responses.TEAM_STATE_RESPONSE,
                            JSONFields.TEAM_STATE: TeamState.LEFT,
                            JSONFields.USERNAME: [game.players[uid].username for uid in game.connected_teams[current_team_id]]
                        })

                    current_user_id = None
                    current_team_id = None
                    current_client_type = None

                elif current_client_type == "player" and (game.game_state == GameState.GAME_RUNNING or game.game_state == GameState.COUNTDOWN):
                    if data.get(JSONFields.TYPE) == GamePlay.PROMPT_OUT:
                        if current_user_id == game.teams[current_team_id].current_turn_uid:
                            await game.teams[current_team_id].input_queue.put(data)
            else:
                await websocket.send_json({JSONFields.TYPE: None})
            
    except WebSocketDisconnect:
        if current_user_id is not None:
            if current_client_type == "admin":
                game.connected_admins.discard(current_user_id)
                game.connected_sockets.pop(current_user_id, None)
            else:
                if current_user_id in game.connected_players:
                    game.connected_players.remove(current_user_id)
                    game.connected_sockets.pop(current_user_id, None)

                if current_team_id is not None and current_user_id in game.connected_teams[current_team_id]:
                    game.connected_teams[current_team_id].remove(current_user_id)
                    game.teams[current_team_id].connected_sockets.pop(current_user_id, None)

                    await game.teams[current_team_id].announce_to_team({
                        JSONFields.TYPE: Responses.TEAM_STATE_RESPONSE,
                        JSONFields.TEAM_STATE: TeamState.LEFT,
                        JSONFields.USERNAME: [game.players[uid].username for uid in game.connected_teams[current_team_id]]
                    })

            current_user_id = None
            current_team_id = None
            current_client_type = None
            
        print("client disconnected")

def verify_admin_session(x_admin_id: str = Header(None, alias="X-Admin-Id")):
    """Dependency to verify that the HTTP request is from an authenticated active admin."""
    if not x_admin_id:
        raise HTTPException(status_code=401, detail="Missing administrative credentials.")
    
    try:
        admin_id = int(x_admin_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid administrator ID format.")
        
    if admin_id not in server.connected_admins:
        raise HTTPException(status_code=403, detail="Unauthorized: Admin session not found or active.")
    
    return admin_id

@app.post("/admin/rungame")
async def force_start_game(admin_id: int = Depends(verify_admin_session), game: GameServer = Depends(get_game_server)):
    # Block starting if the game is already out of the login phase
    if game.game_state != GameState.LOGIN_PERIOD:
        raise HTTPException(status_code=400, detail="Game has already started or concluded.")

    game.game_state = GameState.COUNTDOWN 
    game.countdown_end_time = time.time() + 5

    await game.announce_to_players({
        JSONFields.TYPE: Responses.GAME_STATE_RESPONSE,
        JSONFields.GAME_STATE: GameState.COUNTDOWN,
        JSONFields.TIME: 5 
    })

    await asyncio.sleep(5)
    game.game_state = GameState.GAME_RUNNING
    await game.announce_to_players({
        JSONFields.TYPE: Responses.GAME_STATE_RESPONSE,
        JSONFields.GAME_STATE: GameState.GAME_RUNNING
    })
    
    # Run the game loops in the background so the HTTP response doesn't hang for minutes
    asyncio.create_task(start_games(game, time_per_round=90, timeout=30, penalty=5))
    
    return {"status": "success", "message": "Game countdown initiated successfully."}
#changes have been made

@app.get("/admin/dashboard")
async def get_global_dashboard(admin_id: int = Depends(verify_admin_session), game: GameServer = Depends(get_game_server)):
    connected_teams = sum(1 for team in game.teams if len(team.connected_sockets) > 0)
    return {
        "game_state": game.game_state,
        "total_connected_players": len(game.connected_players),
        "connected_teams": connected_teams,
        "total_teams": len(game.teams),
        "current_round": game.current_round,
        "total_rounds": game.total_rounds
    }

@app.get("/")
async def read_root():
    return FileResponse("static/new_client/webpage.html")