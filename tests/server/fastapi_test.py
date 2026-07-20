from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()

@app.websocket("/ws")
async def handle_client(websocket: WebSocket):
    await websocket.accept()

    try:
        while True:
            data = await websocket.receive_text()
            
            await websocket.send_text(f"Server has recieved: {data}")
    except WebSocketDisconnect:
        print("Client disconnected")

    except RuntimeError:
        print("runtime error")