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

    async def run_game(self, scores,max_members_per_team:int,time_per_round, timeout, penalty):
        no_prompt_penalty = 5
        penalty_multiplier = self.max_members / max(1, len(self.connected_sockets))

        self.rotation_order = list(self.members)
        for self.round in range(5):
            self.current_image = await get_image("default_prompt")
            self.images.append([])
            self.images[self.round].append(self.current_image)
            self.original_image = self.current_image

            for uid in self.rotation_order:
                self.current_turn_uid = uid
                self.turn_end_time = time.time() + time_per_round

                while not self.input_queue.empty():
                    try:
                        self.input_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                
                # Loop through all connected sockets to handle active player vs spectators separately
                for member_uid, socket in self.connected_sockets.items():
                    if member_uid == uid:
                        # 1. The Active Player: Gets the actual gameplay image and turn controls
                        await self.safe_send(member_uid,socket,{
                            JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                            JSONFields.MESSAGE: GamePlay.IMAGE_IN,
                            JSONFields.IS_PLAYER_TURN: True,
                            JSONFields.IMAGE: self.current_image,
                            JSONFields.TIME: time_per_round
                        })
                    else:
                        # 2. Everyone Else: Gets the customized "WAIT_YOUR_TURN_IMAGE" SVG payload
                        await self.safe_send(member_uid,socket,{
                            JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                            JSONFields.MESSAGE: GamePlay.IMAGE_IN,
                            JSONFields.IS_PLAYER_TURN: False,
                            JSONFields.IMAGE: WAIT_YOUR_TURN_IMAGE,
                            JSONFields.TIME: time_per_round
                        })
                while True:
                    try: 
                        time_left = self.turn_end_time - time.time()
                        if time_left > 0:
                            data = await asyncio.wait_for(self.input_queue.get(), timeout=time_left)
                        else:
                            raise asyncio.TimeoutError()
                        #making a set of attempts done by each player in their turn
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
                                # 2. INVALID PROMPT SUBMITTED
                                self.player_attempts[uid] += 1  # Increment the individual player's tracker
                                current_attempts = self.player_attempts[uid]
                                
                                if current_attempts >= 3:
                                    # 3. STRIKE THREE -> FORCED TURN END
                                    if uid in self.connected_sockets:
                                        await self.connected_sockets[uid].send_json({
                                            JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                                            JSONFields.PROMPT_STATUS: False,
                                            # Use .value or the Enum integer explicitly so JS knows what to expect
                                            JSONFields.MESSAGE: GamePlay.OUT_OF_CHANCES.value, 
                                            "attempts_left": 0
                                        })
                                    no_prompt_penalty += penalty * penalty_multiplier
                                    self.player_attempts[uid] = 0
                                    break # Break the while loop to skip to the next player
                                
                                else:
                                    # 4. STILL HAS CHANCES LEFT -> TRY AGAIN
                                    if uid in self.connected_sockets:
                                        await self.connected_sockets[uid].send_json({
                                            JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                                            JSONFields.PROMPT_STATUS: False,
                                            JSONFields.MESSAGE: GamePlay.INVALID_PROMPT,#.value was removed
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
                        no_prompt_penalty += penalty * penalty_multiplier
                        break
                self.prompt_submitted = False
            self.rotation_order = self.rotation_order[1:] + [self.rotation_order[0]]
            

            round_score = await compare_image(no_prompt_penalty,self.original_image,self.current_image)
            self.score.append(round_score)
            await self.announce_to_team({
                JSONFields.TYPE: Responses.GAME_STATE_RESPONSE,
                JSONFields.GAME_STATE: GameState.ROUND_OVER,
                JSONFields.ROUND: self.round + 1,
                JSONFields.ROUND_SCORE: round_score,
                JSONFields.TEAM_SCORE: sum(self.score),
                JSONFields.TIME: 10
            })
            await asyncio.sleep(10)

        self.team_state = TeamState.DONE
        scores[self.id] = sum(self.score)

        await self.announce_to_team({
            JSONFields.TYPE: Responses.TEAM_STATE_RESPONSE,
            JSONFields.TEAM_STATE: TeamState.DONE
        })
