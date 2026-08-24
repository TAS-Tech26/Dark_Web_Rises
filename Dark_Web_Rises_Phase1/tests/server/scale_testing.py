import asyncio
import json
import websockets

TARGET_URL = "ws://127.0.0.1:8000/ws"
NUM_PLAYERS = 600

async def simulate_player(player_id: int):
    username = f"user{player_id}"
    password = f"password{player_id}"

    try:
        async with websockets.connect(TARGET_URL) as ws:
            # 1. Send Login Payload
            login_payload = {
                "type": "login",
                "username": username,
                "password": password
            }
            await ws.send(json.dumps(login_payload))
            
            # 2. Await Response
            raw_response = await ws.recv()
            response = json.loads(raw_response)
            
            # 🔴 DIAGNOSTIC PRINT: See what the server actually replied with
            if response.get("authorised") != "accepted":
                print(f"❌ {username} rejected! Server response: {response}")
                return
            else:
                print(f"✅ {username} logged in successfully!")

            # 3. Stay alive and listen
            async for message in ws:
                data = json.loads(message)
                if data.get("is_player_turn") is True:
                    await asyncio.sleep(1) 
                    turn_payload = {
                        "type": 5, #  Use integer 5 to match GamePlay.PROMPT_OUT
                        "status": "prompted",
                        "prompt": "default_prompt" 
                    }
                    await ws.send(json.dumps(turn_payload))

    except Exception as e:
        print(f"💥 {username} crashed with error: {e}")

async def main():
    print(f"🚀 Spinning up {NUM_PLAYERS} concurrent virtual game clients...")
    tasks = []
    for i in range(NUM_PLAYERS):
        tasks.append(asyncio.create_task(simulate_player(i)))
        await asyncio.sleep(0.05) # Tiny pacing break
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    import sys
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        
    asyncio.run(main())