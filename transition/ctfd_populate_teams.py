import json
import csv

CTFD_URL = "enter ctfd url here"
CTFD_ADMIN_TOKEN = "enter ctfd admin token here"

QUALIFIED_USERS_JSON = "path to json here"

headers = {
    "Authorization": f"Token {CTFD_ADMIN_TOKEN}",
    "Content-Type": "application/json"
}

with open(QUALIFIED_USERS_JSON, "r") as f:
    data = json.load(f)

teams_to_create = {}

for player in data['players']:
    team_name = str(player['team_id']) 
    team_password = player['team_password']
    teams_to_create[team_name] = team_password


with open('users.csv', 'w', newline='') as csvfile:
    fieldnames = ['name', 'email', 'password']
    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

    writer.writeheader()
    
    for team_name, password in teams_to_create.items():
        writer.writerow({
            'name': team_name,
            'email': f"team{team_name}@ctf.local", #fake email 
            'password': password
        })