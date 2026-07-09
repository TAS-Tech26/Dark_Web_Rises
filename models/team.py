import time
import asyncio
from enumerations import JSONFields, Responses, GameState, TeamState, GamePlay
from services.ai_handling import get_image, compare_image, classify_prompt

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
        

    async def announce_to_team(self, message):
        for uid, socket in self.connected_sockets.items():
            await socket.send_json(message)
    async def run_game(self, scores,max_members_per_team:int,time_per_round, timeout, penalty):
        round = 0
        no_prompt_penalty = 5
        penalty_multiplier = self.max_members / max(1, len(self.connected_sockets))

        rotation_order = list(self.members)
        for round in range(5):
            self.current_image = await get_image("default_prompt")
            self.images.append([])
            self.images[round].append(self.current_image)
            self.original_image = self.current_image

            for uid in rotation_order:
                self.current_turn_uid = uid
                self.turn_end_time = time.time() + time_per_round

                if uid in self.connected_sockets:
                    await self.connected_sockets[uid].send_json({
                        JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                        JSONFields.MESSAGE: GamePlay.IMAGE_IN,
                        JSONFields.IS_PLAYER_TURN: True,
                        JSONFields.IMAGE: self.current_image,
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
                                self.current_image = await get_image(prompt=data.get(JSONFields.PROMPT))
                                self.images[round].append(self.current_image)
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
            rotation_order = rotation_order[1:] + [rotation_order[0]]
            

            round_score = await compare_image(no_prompt_penalty,self.original_image,self.current_image)
            self.score.append(round_score)
            await self.announce_to_team({
                JSONFields.TYPE: Responses.GAME_STATE_RESPONSE,
                JSONFields.GAME_STATE: GameState.ROUND_OVER,
                JSONFields.ROUND: round + 1,
                JSONFields.ROUND_SCORE: round_score,
                JSONFields.TEAM_SCORE: sum(self.score),
                JSONFields.TIME: 10
            })
            await asyncio.sleep(10)

        self.team_state = TeamState.DONE

        await self.announce_to_team({
            JSONFields.TYPE: Responses.TEAM_STATE_RESPONSE,
            JSONFields.TEAM_STATE: TeamState.DONE
        })

        scores[self.id] = sum(self.score)