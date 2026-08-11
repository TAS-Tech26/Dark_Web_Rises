import time
import asyncio
from enumerations import JSONFields, Responses, GameState, TeamState, GamePlay
from services.ai_handling import get_image, compare_image, classify_prompt

WAIT_YOUR_TURN_IMAGE = (
    "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='1024' height='1024' viewBox='0 0 1024 1024'>"
    "<rect width='100%' height='100%' fill='%23121214'/>"
    "<text x='50%' y='50%' font-family='monospace' font-size='32' fill='%239ca3af' text-anchor='middle' dominant-baseline='middle'>"
    "WAITING FOR ACTIVE PLAYER TO PROMPT..."
    "</text></svg>"
)

class Team:
    def __init__(self, id, max_members):
        self.team_state = TeamState.WAITING
        self.max_members = max_members
        self.id = id
        self.members = []
        self.connected_sockets = {}
        self.team_name = "insert team name here"
        self.score = []
        self.rank = None

        self.input_queue = asyncio.Queue()
        self.current_image = None
        self.original_image = None
        self.current_turn_uid = None
        self.turn_end_time = 0.0
        self.images = []
        self.prompt_attempts = 0
        self.round = 0
        self.rotation_order = []
        self.prompt_submitted = False

    async def safe_send(self, member_id, socket, payload):
        try:
            # If the TCP buffer is backed up or dead, cut it off after 1.5 seconds
            await asyncio.wait_for(socket.send_json(payload), timeout=1.5)
        except (asyncio.TimeoutError, Exception) as e:
            print(f"Ghost socket detected for {member_id}: {e}")
            if member_id in self.connected_sockets:
                del self.connected_sockets[member_id]

    async def announce_to_team(self, payload):
        tasks = [
            self.safe_send(member_id, socket, payload)
            for member_id, socket in list(self.connected_sockets.items())
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run_round(self, round_num, time_per_round, timeout, penalty):
        no_prompt_penalty = 5

        # 1. Base rotation on assigned members
        connected_uids = [uid for uid in self.members if uid in self.connected_sockets]
        
        # If nobody is connected at all, mark round 0 immediately
        if not connected_uids:
            round_score = 0
            self.score.append(round_score)
            return round_score

        penalty_multiplier = self.max_members / max(1, len(connected_uids))

        self.rotation_order = list(self.members)  # Loop all assigned members
        if round_num != 0:
            shift = round_num % len(self.rotation_order)
            self.rotation_order = self.rotation_order[shift:] + self.rotation_order[:shift]
        
        print(f"Team {self.id} round {round_num + 1} rotation order: {self.rotation_order}")
        self.round = round_num
        self.current_image = await get_image("default_prompt")
        
        while len(self.images) <= self.round:
            self.images.append([])
        self.images[self.round].append(self.current_image)
        self.original_image = self.current_image

        for uid in self.rotation_order:
            self.current_turn_uid = uid
            self.turn_end_time = time.time() + time_per_round

            # Clear leftover inputs from previous turn
            while not self.input_queue.empty():
                try:
                    self.input_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            # --- WINDOW 1: Check connection before turn starts (10s grace period) ---
            if uid not in self.connected_sockets:
                print(f"Player {uid} offline at turn start. Waiting 10s grace period to join...")
                wait_start = time.time()
                reconnected = False
                
                while time.time() - wait_start < 10.0:
                    await asyncio.sleep(0.5)
                    if uid in self.connected_sockets:
                        reconnected = True
                        break
                
                if not reconnected:
                    print(f"Player {uid} failed to connect in 10s. Skipping turn.")
                    no_prompt_penalty += penalty * penalty_multiplier
                    continue

            # Broadcast turn state to team
            for member_uid, socket in list(self.connected_sockets.items()):
                if member_uid == uid:
                    await self.safe_send(member_uid, socket, {
                        JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                        JSONFields.MESSAGE: GamePlay.IMAGE_IN,
                        JSONFields.IS_PLAYER_TURN: True,
                        JSONFields.IMAGE: self.current_image,
                        JSONFields.TIME: max(0.0, self.turn_end_time - time.time())
                    })
                else:
                    await self.safe_send(member_uid, socket, {
                        JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                        JSONFields.MESSAGE: GamePlay.IMAGE_IN,
                        JSONFields.IS_PLAYER_TURN: False,
                        JSONFields.IMAGE: WAIT_YOUR_TURN_IMAGE,
                        JSONFields.TIME: max(0.0, self.turn_end_time - time.time())
                    })

            disconnect_time = None

            while True:
                # --- WINDOW 2: Mid-turn disconnect handling (20s grace period) ---
                if uid not in self.connected_sockets:
                    if disconnect_time is None:
                        disconnect_time = time.time()
                        print(f"Player {uid} disconnected mid-turn! Giving 20s to reconnect...")
                    
                    # Check if 20 seconds have passed since disconnect
                    if time.time() - disconnect_time >= 20.0:
                        print(f"Player {uid} failed to reconnect within 20s.")
                        no_prompt_penalty += penalty * penalty_multiplier
                        break
                else:
                    # Player is back online! Reset disconnect timer
                    if disconnect_time is not None:
                        print(f"Player {uid} reconnected successfully!")
                        disconnect_time = None

                try: 
                    time_left = self.turn_end_time - time.time()
                    if time_left <= 0:
                        raise asyncio.TimeoutError()

                    # Poll queue in short 1-second chunks to monitor disconnect timer
                    data = await asyncio.wait_for(self.input_queue.get(), timeout=min(time_left, 1.0))

                    if not hasattr(self, "player_attempts"):
                        self.player_attempts = {}
                    if uid not in self.player_attempts:
                        self.player_attempts[uid] = 0

                    if data.get(JSONFields.STATUS) == GamePlay.PROMPTED:
                        current_prompt = data.get(JSONFields.PROMPT)
                        is_prompt_valid = classify_prompt(prompt=current_prompt)

                        if is_prompt_valid:
                            self.prompt_submitted = True
                            self.current_image = await get_image(prompt=data.get(JSONFields.PROMPT))
                            self.images[self.round].append(self.current_image)
                            if uid in self.connected_sockets:
                                await self.connected_sockets[uid].send_json({
                                    JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                                    JSONFields.PROMPT_STATUS: is_prompt_valid,
                                    JSONFields.MESSAGE: GamePlay.RECEIVED
                                }) 
                            break
                        else:
                            self.player_attempts[uid] += 1
                            current_attempts = self.player_attempts[uid]
                            
                            if current_attempts >= 3:
                                if uid in self.connected_sockets:
                                    await self.connected_sockets[uid].send_json({
                                        JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                                        JSONFields.PROMPT_STATUS: False,
                                        JSONFields.MESSAGE: GamePlay.OUT_OF_CHANCES.value, 
                                        "attempts_left": 0
                                    })
                                no_prompt_penalty += penalty * penalty_multiplier
                                self.player_attempts[uid] = 0
                                break
                            else:
                                if uid in self.connected_sockets:
                                    await self.connected_sockets[uid].send_json({
                                        JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                                        JSONFields.PROMPT_STATUS: False,
                                        JSONFields.MESSAGE: GamePlay.INVALID_PROMPT,
                                        "attempts_left": 3 - current_attempts 
                                    })
                    elif data.get(JSONFields.STATUS) == GamePlay.NOT_PROMPTED:
                        if uid in self.connected_sockets:
                            await self.connected_sockets[uid].send_json({
                                JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                                JSONFields.MESSAGE: GamePlay.NOT_RECEIVED
                            })
                        no_prompt_penalty += penalty * penalty_multiplier
                        break
                        
                except (asyncio.TimeoutError, TimeoutError):
                    if time.time() >= self.turn_end_time:
                        no_prompt_penalty += penalty * penalty_multiplier
                        break

            self.prompt_submitted = False

        round_score = await compare_image(no_prompt_penalty, self.original_image, self.current_image)
        self.score.append(round_score)
        return round_score