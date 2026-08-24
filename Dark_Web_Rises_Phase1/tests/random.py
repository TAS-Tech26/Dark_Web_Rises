import time
import asyncio
from enumerations import JSONFields, Responses, GameState, TeamState, GamePlay
from services.ai_engine import generate_or_fetch_image, validate_prompt_syntax, compute_similarity_score

class Team:
    def __init__(self, id, max_members, team_name="insert team name here"):
        self.id = id
        self.max_members = max_members
        self.team_name = team_name
        self.team_state = TeamState.WAITING
        self.members = []
        self.connected_sockets = {}
        self.score = []
        self.rank = None
        
        self.input_queue = asyncio.Queue()
        self.current_image = None
        self.original_image = None
        self.current_turn_uid = None
        self.turn_end_time = 0.0
        self.images = []
        self.player_attempts = {}

    async def announce_to_team(self, message: dict):
        for uid, socket in list(self.connected_sockets.items()):
            try:
                await socket.send_json(message)
            except Exception:
                pass

    async def run_game_loop(self, server_scores_ref, max_members_per_team: int, time_per_round=90, penalty=5):
        no_prompt_penalty = 5
        
        # 5-Round Sequence Tracker
        for round_idx in range(5):
            penalty_multiplier = max_members_per_team / max(1, len(self.connected_sockets))
            self.current_image = await generate_or_fetch_image("default_prompt")
            self.images.append([self.current_image])
            self.original_image = self.current_image

            for uid in self.members:
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
                        if time_left <= 0:
                            raise asyncio.TimeoutError()
                        
                        data = await asyncio.wait_for(self.input_queue.get(), timeout=time_left)
                        
                        if uid not in self.player_attempts:
                            self.player_attempts[uid] = 0

                        if data.get(JSONFields.STATUS) == GamePlay.PROMPTED:
                            current_prompt = data.get(JSONFields.PROMPT)
                            is_prompt_valid = validate_prompt_syntax(current_prompt)

                            if is_prompt_valid:
                                self.current_image = await generate_or_fetch_image(prompt=current_prompt)
                                self.images[round_idx].append(self.current_image)
                                if uid in self.connected_sockets:
                                    await self.connected_sockets[uid].send_json({
                                        JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                                        JSONFields.PROMPT_STATUS: True,
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
                                            JSONFields.MESSAGE: GamePlay.OUT_OF_CHANCES,
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
                    except asyncio.TimeoutError:
                        no_prompt_penalty += penalty * penalty_multiplier
                        break

            # Calculate and broadcast the round outcome
            round_score = await compute_similarity_score(no_prompt_penalty, self.original_image, self.current_image)
            self.score.append(round_score)
            
            await self.announce_to_team({
                JSONFields.TYPE: Responses.GAME_STATE_RESPONSE,
                JSONFields.GAME_STATE: GameState.ROUND_OVER,
                JSONFields.ROUND: round_idx + 1,
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
        server_scores_ref[self.id] = sum(self.score)