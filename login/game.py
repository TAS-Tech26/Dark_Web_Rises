from fastapi import FastAPI, WebSocket, Depends, WebSocketDisconnect
from GameServer import GameServer, User, Team
from enumerations import JSONFields, Login, NameStatus
import json

server = GameServer(server_id=0,
                    socket = None,
                    max_teams=3,
                    max_members_per_team=4,
                    team_names=["name1", "name2", "name3"],
                    user_data={ 
                        0:["user1", "password1", 0],
                        1:["user2", "password2", 0],
                        2:["user3", "password3", 2]
                    })

def get_game_server():
    return server

app = FastAPI()

@app.websocket("/ws")
async def game_endpoint(websocket: WebSocket, game: GameServer = Depends(get_game_server)):
    game.socket = websocket

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

            elif current_user_id != None:
                #logout check
                if data.get(JSONFields.TYPE) == Login.LOGOUT:
                    response = game.check_logout(user_id=current_user_id)

                    await websocket.send_json(response)

                    current_user_id = None
                    current_team_id = None

            else:
                await websocket.send_json({JSONFields.TYPE: None})
            
    except WebSocketDisconnect:
        if current_user_id is not None:
            if current_user_id in game.connected_users:
                game.save_user_data(current_user_id)
                game.connected_users.remove(current_user_id)

            if current_user_id in game.connected_teams[current_team_id]:
                game.connected_teams[current_team_id].remove(current_user_id)
        print("client disconnected")