from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
import json
import base64
from io import BytesIO
from huggingface_hub import AsyncInferenceClient
import compare as cmp
import asyncio
#most of the types of responses that i think wud be sent to the players during the whole round
'''
    response_1 = json.dumps({
    "type": "your_turn",
    "message": "enter a prompt that can generate an image with highest accuracy",
    "image": game_states[room_id]["images"][-1][game_states[room_id]["play_counter"]]  # the current image to describe
    })
    response_2 = json.dumps({
        "type": "text",
        "message": f"waiting for player{game_states[room_id]['play_counter']+1} to prompt"
    })
    response_3 = json.dumps({
        "type": "text",
        "message": f"round{game_states[room_id]['round_counter']+1} is starting"
    })
    response_4 = json.dumps({
    "type": "text",
    "message": "game over",
    "score": final_score
    })
'''

'''game_states={
    "room_id_1":{
        "play_counter":0,
        "images":[[],[],[],[],[]],#each nested list represents each round
        "prompts":[],
        "round_counter":0,
        "score":[]
    }
}
connections = {
    "room1": {
        "player1_id": websocket1,
        "player2_id": websocket2,
        "player3_id": websocket3,
        "player4_id": websocket4,
    }
}'''

game_states={}
connections = {}

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

#this is the Image Gen APi from hugging face, currently from my account running on free credits which wud expire soon
client = AsyncInferenceClient(
    provider="nscale",
    api_key="HF_TOKEN",
    timeout=120
)

#initiating each game rooms image list with the predefined image, for now its just one later we can have a set of them and load them randomly
@app.post("/rooms/{room_id}/init")
def init_room(room_id: str):
    game_states[room_id] = {
        "play_counter": 0,
        "images": [],
        "prompts": [],
        "round_counter": 0,
        "score": []
    }
    game_states[room_id]["images"].append(["http://localhost:8000/static/test_image.jpg"])
    return {"status": "ok"}

@app.websocket("/ws/{room_id}/{player_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, player_id: str):
    await websocket.accept()
    print(f"game_states: {game_states}")
    if room_id not in game_states:
        await websocket.send_text(json.dumps({"type": "error", "message": "room does not exist"}))
        await websocket.close()
        return
    if room_id not in connections:
        connections[room_id] = {}
    connections[room_id][player_id] = websocket
    print(f"Room {room_id} players: {list(connections[room_id].keys())}")
    print(f"Total: {len(connections[room_id])}")

    # check if all players have joined
    if len(connections[room_id]) == 4:  #each room has 4 players
        current_player = f"player{game_states[room_id]['play_counter']+1}_id"
        #only the first player will be getting the preloaded image while the rest will be blank for now and waiting for them to prompt
        await connections[room_id][current_player].send_text(json.dumps({
            "type": "your_turn",
            "message": "enter a prompt that can generate an image with highest accuracy",
            "image": game_states[room_id]["images"][0]
        }))
        for pid, ws in connections[room_id].items():
            if pid != current_player:
                await ws.send_text(json.dumps({
                    "type": "text",
                    "message": "waiting for player1 to prompt"
                }))
    
    try:
        while True:
            data = await websocket.receive_text()
            await handle_room_message(room_id, player_id, data)
    except WebSocketDisconnect:
        print(f"{player_id} disconnected")
        del connections[room_id][player_id]
    except Exception as e:
        print(f"Error for {player_id}: {e}")  # add this

#this function is to handle wtvr must be done after recieving the first prompt from the player 1
async def handle_room_message(room_id,player_id,data):
    final_score = 0 #unncessary
    print(f"handle_room_message called: {room_id}, {player_id}, {data}")
    current_player = f"player{game_states[room_id]['play_counter']+1}_id"
    print(f"current_player: {current_player}, player_id: {player_id}")
    if player_id!=current_player:
        print("not current player, returning")
        return
    asyncio.create_task(generate_and_send(room_id, player_id, data, current_player))#asyncio to enable simultaneous image genaration and round movements

async def generate_and_send(room_id, player_id, data, current_player):
    next_player = f"player{game_states[room_id]['play_counter']+2}_id"
    print("generating image...")
    image = await client.text_to_image(
        data,
        model="black-forest-labs/FLUX.1-schnell" 
    )
    print("image genarated")
    # convert PIL image to base64
    buffer = BytesIO()
    image.save(buffer, format="JPEG")
    image_base64 = base64.b64encode(buffer.getvalue()).decode()

    game_states[room_id]["images"][-1].append(f"data:image/jpeg;base64,{image_base64}")
    game_states[room_id]["play_counter"]+=1
    print(f"play_counter: {game_states[room_id]['play_counter']}")

    #if there are more players left to prompt
    if game_states[room_id]["play_counter"] < 4:
        print("sending to next player...")
        for player_id, ws in connections[room_id].items():
            if player_id == next_player:#the next player will be getting the image that has been genarated from the prev_player(current_player)'s prompt
                await ws.send_text(json.dumps({
                    "type": "your_turn",
                    "message": "enter a prompt that can generate an image with highest accuracy",
                    "image": game_states[room_id]["images"][-1][game_states[room_id]["play_counter"]] 
                }))
            else:
                await ws.send_text(json.dumps({
                    "type": "text",
                    "message": f"waiting for player{game_states[room_id]['play_counter']+1} to prompt"
                }))

    #one round has ended, so the score will be calculated, play_counter will be resent and round_counter will be incemented
    elif game_states[room_id]["play_counter"] == 4:
        print("end of round triggered!")

        score = cmp.get_score("http://localhost:8000/static/test_image.jpg",f"data:image/jpeg;base64,{image_base64}")
        game_states[room_id]["score"].append(score)
        game_states[room_id]["play_counter"] = 0
        game_states[room_id]["round_counter"] += 1

        print(game_states[room_id]["score"])
        print(f"round_counter: {game_states[room_id]['round_counter']}")

        #if a round is over and the whole game is over
        if game_states[room_id]["round_counter"] == 5:
            print("5 rounds have gotten over")
            final_score = sum(game_states[room_id]["score"])
            for player_id, ws in connections[room_id].items():
                await ws.send_text(json.dumps({
                    "type": "text",
                    "message": "game over",
                    "score": final_score
                }))
        
        #a round is over but there are more rounds left
        else:
            current_player = f"player{game_states[room_id]['play_counter']+1}_id"
            next_player = f"player{game_states[room_id]['play_counter']+2}_id"
            game_states[room_id]["images"].append(["http://localhost:8000/static/test_image.jpg"])
            print("should be restarting for the next round right now")
            for player_id, ws in connections[room_id].items():
                await ws.send_text(json.dumps({
                    "type": "text",
                    "message": f"round{game_states[room_id]['round_counter']+1} is starting"
                }))
            await connections[room_id][current_player].send_text(json.dumps({
                "type": "your_turn",
                "message": "enter a prompt that can generate an image with highest accuracy",
                "image": game_states[room_id]["images"][-1][game_states[room_id]["play_counter"]] # the current image to describe
            }))
            for player_id, ws in connections[room_id].items():
                if player_id != current_player:
                    await ws.send_text(json.dumps({
                        "type": "text",
                        "message": f"waiting for player{game_states[room_id]['play_counter']+1} to prompt"
                    }))
        