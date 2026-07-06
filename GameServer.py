from enumerations import JSONFields, Login, NameStatus, Responses, GameState, TeamState, GamePlay
from fastapi import FastAPI, WebSocket, Depends, WebSocketDisconnect
import time
import json
import httpx
import asyncio
import random
import os
from huggingface_hub import AsyncInferenceClient
from io import BytesIO
import base64
import open_clip
from PIL import Image
import torch
import re
import nltk
from nltk.corpus import words

nltk.download('words', quiet=True)
english_words = set(w.lower() for w in words.words())

COMMON_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "can", "could", "should", "may", "might", "shall",
    "i", "you", "he", "she", "it", "we", "they",
    "this", "that", "these", "those",
    "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "and", "but", "or", "so", "because", "if", "then",
    "my", "your", "his", "her", "its", "our", "their",
    "what", "who", "where", "when", "how", "why"
}

image_comparator, _, preprocess = open_clip.create_model_and_transforms('ViT-B-32', pretrained='laion2b_s34b_b79k')
image_comparator.eval()
custom_timeout = httpx.Timeout(60.0, connect=10.0, read=None, write=20.0)
imagegen_client = AsyncInferenceClient(
        model="stabilityai/stable-diffusion-xl-base-1.0",  # Free tier friendly
        token="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",            # Explicit token argument
        timeout=custom_timeout
    )

def load_image(img_input):
    if isinstance(img_input, str) and img_input.startswith("data:image"):
        img_input = img_input.split("base64,")[1]
        return Image.open(BytesIO(base64.b64decode(img_input)))
    elif isinstance(img_input, str) and img_input.startswith("http://127.0.0.1"):
        # load from file directly instead of HTTP
        path = img_input.replace("http://127.0.0.1:8000/", "")
        return Image.open(path)
    else:
        return Image.open(img_input)
    
async def get_image(prompt):
    if prompt == "default_prompt":
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
        
        return f"http://127.0.0.1:8000/static/images/{random_filename}"
    else:
        image = await imagegen_client.text_to_image(
        prompt,
        model="black-forest-labs/FLUX.1-schnell" 
        )
        buffer = BytesIO()
        image.save(buffer, format="JPEG")
        image_base64 = base64.b64encode(buffer.getvalue()).decode()
        return f"data:image/jpeg;base64,{image_base64}"

async def compare_image(penalty, original, new):
    image1 = preprocess(load_image(original)).unsqueeze(0)
    image2 = preprocess(load_image(new)).unsqueeze(0)
    
    with torch.no_grad(), torch.amp.autocast('cpu'):
        feat1 = image_comparator.encode_image(image1)
        feat2 = image_comparator.encode_image(image2)
        feat1 /= feat1.norm(dim=-1, keepdim=True)
        feat2 /= feat2.norm(dim=-1, keepdim=True)
        similarity = (feat1 @ feat2.T).item()
    
    sim_clipped = max(0, min(similarity, 1))
    return round(sim_clipped * 100, 2)- penalty

def classify_prompt(prompt):
    if not prompt or not prompt.strip():
        return False
    
    words_list = prompt.strip().lower().split()
    
    if len(words_list) < 4:
        return False
    
    letters = sum(c.isalpha() for c in prompt)
    if letters / len(prompt) < 0.6:
        return False
    
    if re.search(r'(.)\1{4,}', prompt):
        return False
    
    if not any(word in COMMON_WORDS for word in words_list):
        return False
    
    # stricter check for longer prompts
    if len(words_list) >= 8:
        real_words = sum(1 for w in words_list if w in english_words)
        if real_words / len(words_list) < 0.5:
            return False
    
    return True


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
    
    def rank_teams(self):
        sorted_scores = sorted(self.scores.items(), key=lambda x: x[1], reverse=True)
        top3 = sorted_scores[:3]

        for i, (tid, score) in enumerate(sorted_scores):
           self.teams[tid].rank = i 

        return top3
                                                 



#changes have been made