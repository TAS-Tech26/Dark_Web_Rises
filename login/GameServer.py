from enumerations import JSONFields, Login, NameStatus, Responses, GameState, TeamState

class User:
    def __init__(self, username, id, password):
        self.username = username
        self.id = id
        self.team_id = None
        self.password = password
        
class Team:
    def __init__(self, id, max_members):
        self.team_state = TeamState.NOT_READY
        self.max_members = max_members
        self.id = id
        self.members = []
        self.connected_sockets = {}
        self.team_name = "insert team name here"


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
                JSONFields.USER_ID: None
                }
        
        username = data.get(JSONFields.USERNAME)
        pwd = data.get(JSONFields.PASSWORD)

        for user in self.users:
            if username == user.username:
                response[JSONFields.STATUS] = NameStatus.AVAILABLE

                if user.password == pwd:
                    uid = user.id
                    tid = user.team_id

                    if len(self.connected_teams[tid]) < 4 and uid not in self.connected_users:
                        response[JSONFields.AUTHORISED] = Login.ACCEPTED
                        response[JSONFields.USER_ID] = uid
                        
                        self.connected_users.add(uid)
                        self.connected_sockets[uid] = socket

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

            self.save_user_data(user_id)
            self.connected_users.remove(user_id)

            self.connected_sockets.pop(user_id)

            team_id = self.users[user_id].team_id
            if user_id in self.connected_teams[team_id]:
                self.connected_teams[team_id].remove(user_id)
                self.teams[team_id].connected_sockets.pop(user_id)
            
            team_unready = 0

            if len(self.connected_teams[team_id]) == 3:
                team_unready = 1

        return response, team_unready
    
    async def announce_to_users(self, message):
        for uid, socket in self.connected_sockets:
            await socket.send_json(message)
    
    def save_user_data(self, user_id):
        #to be done later
        pass



