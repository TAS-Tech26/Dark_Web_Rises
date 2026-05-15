import asyncio
import websockets
import json
from enumerations import JSONFields, Login, NameStatus
import random

class User:
    def __init__(self, username, id):
        self.username = username
        self.id = id
        self.team_id = None
        
class Team:
    def __init__(self, id, max_members, team_name_change_attempts):
        self.team_name_change_attempts = team_name_change_attempts
        self.current_attempts = 0
        self.max_members = max_members
        self.id = id
        self.members = []
        self.team_name = "insert team name here"

class GameServer:
    def __init__(self, server_id, max_teams, max_members_per_team, team_names): #team names later
        self.server_id = server_id
        self.max_members_per_team = max_members_per_team
        self.users = [None] * (max_teams*max_members_per_team)

        self.teams = []

        for i in range(max_teams):
            new_team = Team(id=i, max_members=max_members_per_team, team_name_change_attempts=2)
            new_team.team_name = team_names[i]
            self.teams.append(new_team)
        

    async def handle_client(self, websocket):
        user_id = 0
        try:
            async for message in websocket:
                try:
                    init_response = {
                        JSONFields.TYPE: None,
                        JSONFields.STATUS: NameStatus.AVAILABLE,
                        JSONFields.ID: None
                    }
                    data = json.loads(message)

                    if data.get(JSONFields.STATUS) == Login.LOGIN:
                        name = data.get(JSONFields.USERNAME)
                        init_response[JSONFields.TYPE] = Login.LOGIN_RESPONSE

                        for user in self.users:
                            if name == user.username:
                                init_response[JSONFields.STATUS] = NameStatus.TAKEN
                                break

                        if init_response[JSONFields.STATUS] == NameStatus.AVAILABLE:
                            u = User(name, user_id)
                            init_response[JSONFields.ID] = user_id
                            self.users[user_id] = u
                            user_id += 1

                    elif data.get(JSONFields.TYPE) == Login.JOIN_TEAM:
                        user_id = data.get(JSONFields.USER_ID)
                        init_response[JSONFields.STATUS] = NameStatus.TAKEN

                        if (self.users[user_id].team_id == None):
                            selected_team = None

                            for team in self.teams:
                                if len(team.members) == 0:
                                    selected_team = team
                                    init_response[JSONFields.STATUS] = NameStatus.AVAILABLE
                                    break

                            if init_response[JSONFields.STATUS] == NameStatus.AVAILABLE:
                                selected_team.members.append(user_id)
                                self.user[user_id].team_id = selected_team.id

                    elif data.get(JSONFields.TYPE) == Login.SELECT_TEAM:   
                        user_id = data.get(JSONFields.USER_ID)
                        init_response[JSONFields.STATUS] = NameStatus.DNE

                        if (self.users[user_id].team_id == None):
                            name = data.get(JSONFields.TEAM_NAME)

                            init_response[JSONFields.TYPE] = Login.SELECT_TEAM_RESPONSE

                            selected_team = None

                            for team in self.teams:
                                if name == team.team_name:
                                    init_response[JSONFields.STATUS] = NameStatus.AVAILABLE
                                    selected_team = team
                                    break
                            
                            if len(selected_team.members) >= selected_team.max_members:
                                init_response[JSONFields.STATUS] = NameStatus.TAKEN

                            if init_response[JSONFields.STATUS] == NameStatus.AVAILABLE:
                                selected_team.members.append(user_id)
                                self.users[user_id].team_id = selected_team.id

                    elif data.get(JSONFields.TYPE) == Login.CHANGE_TEAM_NAME:
                        team_id = data.get(JSONFields.TEAM_ID)
                        new_team_name = data.get(JSONFields.TEAM_NAME)

                        for team in self.teams:
                            if new_team_name == team.team_name:
                                init_response[JSONFields.STATUS] = NameStatus.TAKEN
                                break
                        
                        if init_response[JSONFields.STATUS] == NameStatus.AVAILABLE:
                            self.teams[team_id].team_name = new_team_name
                            

                    websocket.send(json.dumps(init_response))

                except json.JSONDecodeError:
                    print("json decoding error")

        except websockets.exceptions.ConnectionClosed:
            print("The browser disconnected.")
        finally:
            print("The browser disconnected.")



