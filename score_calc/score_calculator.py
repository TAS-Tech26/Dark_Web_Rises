import requests
import json

MAX_POINTS_CTF = 430
MAX_POINTS_ROUND1 = 500

CTFD_URL = "enter CTFd url here"
CTFD_ADMIN_TOKEN = "ctfd token here"

#sorted in order of score 
PHASE1_STATE = "here phase 1 state json here"
LAST_COMPLETED_ROUND = "4" #i guess

headers = {
    "Authorization": CTFD_ADMIN_TOKEN,
    "Content-Type": "application/json"
}

response = requests.get(CTFD_URL, headers=headers)
scoreboard_data = response.json()

ctf_team_dict = {}
round1_team_dict = {}

for team in scoreboard_data['data']:
                                    #normalise score
    ctf_team_dict[team['name']] = team['score']/MAX_POINTS_CTF

with open(PHASE1_STATE, "r") as f:
    phase1_data = json.load(f)

    for team_id, score in phase1_data['rounds'][LAST_COMPLETED_ROUND].items():
        round1_team_dict[team_id] = score/MAX_POINTS_ROUND1

combined_team_scores = {}


for team_id, phase1_score in round1_team_dict.items():
    
    ctf_score = ctf_team_dict.get(team_id, 0)

                                    #score out of 100 (norm1 + norm2)/2 * 100
    combined_team_scores[team_id] = (phase1_score + ctf_score) * 50
                                                                                            
final_scoreboard = dict(sorted(combined_team_scores.items(), key=lambda item: item[1], reverse=True))











