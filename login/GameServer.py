class User:
    def __init__(self, username, id, password):
        self.username = username
        self.id = id
        self.team_id = None
        self.password = password
        
class Team:
    def __init__(self, id, max_members):
        self.current_attempts = 0
        self.max_members = max_members
        self.id = id
        self.members = []
        self.team_name = "insert team name here"

class GameServer:
    def __init__(self, server_id, max_teams, max_members_per_team, team_names, user_data): #team names later
        self.server_id = server_id
        self.max_members_per_team = max_members_per_team

        self.users = []
        for user_id, info in user_data.items():
            name = info[0]
            pwd = info[1]
            team_id = info[2]

            new_user = User(username=name, id=user_id, password=pwd)
            new_user.team_id = team_id
            self.users.append(new_user)

        self.teams = []

        for i in range(max_teams):
            new_team = Team(id=i, max_members=max_members_per_team)
            new_team.team_name = team_names[i]
            self.teams.append(new_team)



