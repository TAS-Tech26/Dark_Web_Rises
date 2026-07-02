import os
import base64
import json
from io import BytesIO
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from huggingface_hub import AsyncInferenceClient

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

client = AsyncInferenceClient(
    provider="nscale",
    api_key="HF_TOKEN"
)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("connection open")
    try:
        while True:
            data = await websocket.receive_text()
            print(f"Received: {data}")
            image = await client.text_to_image(
                data,
                model="black-forest-labs/FLUX.1-schnell"  # fast flux model 
            )
            # convert PIL image to base64
            buffer = BytesIO()
            image.save(buffer, format="JPEG")
            image_base64 = base64.b64encode(buffer.getvalue()).decode()
            await websocket.send_text(json.dumps({
                "type": "image",
                "url": f"data:image/jpeg;base64,{image_base64}"
            }))
    except WebSocketDisconnect:
        print("Client disconnected")