from fastapi import FastAPI, WebSocket, Depends, WebSocketDisconnect
from GameServer import GameServer, User, Team
from enumerations import JSONFields, Login, NameStatus
import json

import auth

server = GameServer(server_id=0,
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
    await websocket.accept()
    current_user_id = None
    try:
        while True:
            data = await websocket.receive_json() 

            """data format:
                TYPE: <some type (LOGIN/GAMEPLAY etc)>
                <required fields under that type>
            """

            if data.get(JSONFields.TYPE) == Login.LOGIN:
                response = auth.check_login(data, game.users)

                if response.get(JSONFields.AUTHORISED) == Login.ACCEPTED:
                    current_user_id = response.get(JSONFields.ID)

                await websocket.send_json(response)

            elif current_user_id != None:
                #game here
                pass
            

            else:
                await websocket.send_json({JSONFields.TYPE: None})
            
    except WebSocketDisconnect:
        print("client disconnected")