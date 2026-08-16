import requests
import json

MAX_POINTS_CTF = 430
MAX_POINTS_PHASE1 = 500

CTFD_URL = "enter CTFd url here"
CTFD_ADMIN_TOKEN = "ctfd token here"

#sorted in order of score 
PHASE1_STATE = "here phase 1 state json here"
LAST_COMPLETED_ROUND = "4" #i guess

SCORES_OUTPUT_PATH = "enter path to scores json here"

headers = {
    "Authorization": CTFD_ADMIN_TOKEN,
    "Content-Type": "application/json"
}

response = requests.get(CTFD_URL, headers=headers)
scoreboard_data = response.json()

ctf_team_dict = {}
phase1_team_dict = {}

for team in scoreboard_data['data']:
                                    #normalise score
    ctf_team_dict[team['name']] = team['score']/MAX_POINTS_CTF

with open(PHASE1_STATE, "r") as f:
    phase1_data = json.load(f)

    for team_id, score in phase1_data['rounds'][LAST_COMPLETED_ROUND].items():
        phase1_team_dict[team_id] = score/MAX_POINTS_PHASE1

combined_team_scores = {}


for team_id, phase1_score in phase1_team_dict.items():
    
    ctf_score = ctf_team_dict.get(team_id, 0)

                                    #score out of 100 (norm1 + norm2)/2 * 100
    combined_team_scores[team_id] = (phase1_score + ctf_score) * 50
                                                                                            
final_scoreboard = dict(sorted(combined_team_scores.items(), key=lambda item: item[1], reverse=True))


export_data = {
     "ctf": [
            {"team": team, "raw_score": round(score * MAX_POINTS_CTF), "normalized": round(score, 2)}
            for team, score in ctf_team_dict.items()
        ],
    "round1": [
        {"team": team, "raw_score": round(score * MAX_POINTS_PHASE1), "normalized": round(score, 2)}
        for team, score in phase1_team_dict.items()
    ],
    "combined": [
        {"team": team, "score": round(score, 2)}
        for team, score in final_scoreboard.items()
    ],
}

with open(SCORES_OUTPUT_PATH, "w") as f:
    json.dump(export_data, f, indent=2)











