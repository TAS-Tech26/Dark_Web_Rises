from enumerations import JSONFields, Login, NameStatus, Responses, GameState, TeamState, GamePlay
from fastapi import FastAPI, WebSocket, Depends, WebSocketDisconnect
import time
import json
import asyncio
import random
import os

async def get_image(prompt):
    folder_path = os.path.join("static", "images")
    try:
        all_files = os.listdir(folder_path)
    except FileNotFoundError:
        print("folder path doesnt exist")
        return "/static/default.png"
    
    valid_extensions = (".png", ".jpeg", ".jpg", ".gif")

    image_files = [f for f in all_files if f.lower().endswith(valid_extensions)]

    if not image_files:
        print("no images in folder")
        return "/static/default.png"

    random_filename = random.choice(image_files)
    
    return f"http://127.0.0.1:65432/static/images/{random_filename}"

async def compare_image(penalty, original=None, new=None):
    return random.randint(100, 400) - penalty

def classify_prompt(prompt):
    return random.choice([True, False])


class User:
    def __init__(self, username, id, password):
        self.username = username
        self.id = id
        self.team_id = None
        self.password = password
        
class Team:
    def __init__(self, id, max_members):
        self.team_state = TeamState.WAITING
        self.max_members = max_members
        self.id = id
        self.members = []
        self.connected_sockets = {}
        self.team_name = "insert team name here"
        self.score = 0
        self.rank = None

        self.input_queue = asyncio.Queue()
        self.current_image = None
        self.current_turn_uid = None
        self.turn_end_time = 0.0
        

    async def announce_to_team(self, message):
        for uid, socket in self.connected_sockets.items():
            await socket.send_json(message)


class GameServer:
    def __init__(self, server_id, max_teams, max_members_per_team, team_names, user_data): #team names later
        self.server_id = server_id

        self.game_state = GameState.LOGIN_PERIOD

        self.max_members_per_team = max_members_per_team

        self.connected_users = set()
        self.connected_teams = [[] for _ in range(max_teams)]
        self.connected_sockets = {}

        self.game_state = GameState.LOGIN_PERIOD
        self.countdown_end_time = 0.0

        self.scores = {}

        self.teams = []

        for i in range(max_teams):
            new_team = Team(id=i, max_members=max_members_per_team)
            new_team.team_name = team_names[i]
            self.teams.append(new_team)

        self.users = []

        for user_id, info in user_data.items():
            name = info[0]
            pwd = info[1]
            team_id = info[2]
            self.teams[team_id].members.append(user_id)
            new_user = User(username=name, id=user_id, password=pwd)
            new_user.team_id = team_id
            self.users.append(new_user)

    def check_login(self, data, socket):
        response = {
                JSONFields.TYPE: Responses.LOGIN_RESPONSE,
                JSONFields.STATUS: NameStatus.DNE,
                JSONFields.AUTHORISED: Login.DENIED,
                JSONFields.CONNECTED_TEAM_MEMBERS: None,
                JSONFields.USER_ID: None,
                JSONFields.TEAM_STATE: None,

                JSONFields.IS_PLAYER_TURN: False,
                JSONFields.TIME: None,
                JSONFields.IMAGE: None,
                JSONFields.GAME_STATE: self.game_state
                }
        
        username = data.get(JSONFields.USERNAME)
        pwd = data.get(JSONFields.PASSWORD)

        for user in self.users:
            if username == user.username:
                response[JSONFields.STATUS] = NameStatus.AVAILABLE

                if user.password == pwd:
                    uid = user.id
                    tid = user.team_id

                    target_team = self.teams[tid]

                    if len(self.connected_teams[tid]) < 4 and uid not in self.connected_users:
                        response[JSONFields.AUTHORISED] = Login.ACCEPTED
                        response[JSONFields.USER_ID] = uid
                        response[JSONFields.TEAM_STATE] = target_team.team_state

                        if target_team.team_state == TeamState.PLAYING:
                            response[JSONFields.IMAGE] = target_team.current_image

                            if target_team.current_turn_uid == uid:
                                time_left = max(0, target_team.turn_end_time - time.time())
                                response[JSONFields.IS_PLAYER_TURN] = True
                                response[JSONFields.TIME] = time_left
                        

                        if target_team.team_state == TeamState.DONE:
                            response[JSONFields.TEAM_SCORE] = target_team.score
                            response[JSONFields.TEAM_RANK] = target_team.rank

                        if self.game_state == GameState.COUNTDOWN:
                            time_left = max(0, self.countdown_end_time - time.time())
                            response[JSONFields.TIME] = time_left
                        
                        if self.game_state == GameState.GAME_OVER:
                            response[JSONFields.TOP3] = self.rank_teams()

                        self.connected_users.add(uid)
                        self.connected_sockets[uid] = socket
                        self.teams[tid].connected_sockets[uid] = socket 

                        if user.id not in self.connected_teams[tid]:
                            self.connected_teams[tid].append(uid)
                            response[JSONFields.CONNECTED_TEAM_MEMBERS] = len(self.connected_teams[tid])

                break

        return response
    
    def check_logout(self, user_id):
        response = {
            JSONFields.TYPE: Responses.LOGOUT_RESPONSE,
            JSONFields.STATUS: NameStatus.DNE,
            JSONFields.AUTHORISED: Login.DENIED
        }

        if user_id in self.connected_users:
            response[JSONFields.AUTHORISED] = Login.ACCEPTED
            response[JSONFields.STATUS] = NameStatus.AVAILABLE

            self.connected_users.remove(user_id)

            self.connected_sockets.pop(user_id)

            team_id = self.users[user_id].team_id
            if user_id in self.connected_teams[team_id]:
                self.connected_teams[team_id].remove(user_id)
                self.teams[team_id].connected_sockets.pop(user_id)

        return response
    
    async def announce_to_users(self, message):
        for uid, socket in self.connected_sockets.items():
            await socket.send_json(message)
    

    async def start_games(self, time_per_round, timeout, penalty):
        tasks = []
        
        for team in self.teams:
            if len(team.connected_sockets) > 0:
                team.team_state = TeamState.PLAYING
                task = asyncio.create_task(self.run_game(team, time_per_round, timeout, penalty))
                tasks.append(task)

            else:
                team.team_state = TeamState.DONE
                team.score = -1 
                self.scores[team.id] = -1

        if tasks: 
            await asyncio.gather(*tasks)

            print("All teams finished the game")        

        self.game_state = GameState.GAME_OVER

        
        top3 = self.rank_teams()

        for team in self.teams:
            await team.announce_to_team({
                JSONFields.TYPE: Responses.GAME_STATE_RESPONSE,
                JSONFields.GAME_STATE: GameState.GAME_OVER,
                JSONFields.TEAM_SCORE: team.score,
                JSONFields.TEAM_RANK: team.rank,
                JSONFields.TOP3: top3
            })

    async def run_game(self, team: Team, time_per_round, timeout, penalty):
        no_prompt_penalty = 0
        penalty_multiplier = self.max_members_per_team/len(team.connected_sockets)

        team.current_image = await get_image("default_prompt")

        for uid in team.members:
            team.current_turn_uid = uid
            team.turn_end_time = time.time() + time_per_round

            if uid in team.connected_sockets:
                await team.connected_sockets[uid].send_json({
                    JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                    JSONFields.MESSAGE: GamePlay.IMAGE_IN,
                    JSONFields.IS_PLAYER_TURN: True,
                    JSONFields.IMAGE: team.current_image,
                    JSONFields.TIME: time_per_round
                })

            try: 
                time_left = team.turn_end_time - time.time()

                if time_left > 0:
                    data = await asyncio.wait_for(team.input_queue.get(), timeout=time_left)

                else:
                    raise asyncio.TimeoutError()

                if data.get(JSONFields.STATUS) == GamePlay.PROMPTED:

                    current_prompt = data.get(JSONFields.PROMPT)

                    is_prompt_valid = classify_prompt(prompt=current_prompt)

                    if is_prompt_valid:
                        team.current_image = await get_image(prompt=data.get(JSONFields.PROMPT))

                    if uid in team.connected_sockets:
                        await team.connected_sockets[uid].send_json({
                            JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                            JSONFields.PROMPT_STATUS: is_prompt_valid,
                            JSONFields.MESSAGE: GamePlay.RECEIVED
                        }) 

                elif data.get(JSONFields.STATUS) == GamePlay.NOT_PROMPTED:
                    if uid in team.connected_sockets:
                        await team.connected_sockets[uid].send_json({
                            JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                            JSONFields.MESSAGE: GamePlay.NOT_RECEIVED
                        })

                        no_prompt_penalty += penalty * penalty_multiplier
            except asyncio.TimeoutError:
                no_prompt_penalty += penalty * penalty_multiplier
                continue

        team.team_state = TeamState.DONE

        await team.announce_to_team({
            JSONFields.TYPE: Responses.TEAM_STATE_RESPONSE,
            JSONFields.TEAM_STATE: TeamState.DONE
        })

        team_score = await compare_image(no_prompt_penalty)
        team.score = team_score
        self.scores[team.id] = team_score


    def rank_teams(self):
        sorted_scores = sorted(self.scores.items(), key=lambda x: x[1], reverse=True)
        top3 = sorted_scores[:3]

        for i, (tid, score) in enumerate(sorted_scores):
           self.teams[tid].rank = i 

        return top3
                                                 



