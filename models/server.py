import time
from enumerations import JSONFields, Login, NameStatus, Responses, GameState, TeamState, GamePlay
from models.player import User
from models.team import Team, WAIT_YOUR_TURN_IMAGE

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
                            is_active_player = (target_team.current_turn_uid == uid)
                            if is_active_player:
                                resolved_image = target_team.current_image
                            else:
                                resolved_image = WAIT_YOUR_TURN_IMAGE

                            response[JSONFields.IMAGE] = resolved_image
                            response[JSONFields.TIME] = max(0.0, target_team.turn_end_time - time.time())
                            response[JSONFields.TEAM_STATE] = target_team.team_state

                            if is_active_player:
                                response[JSONFields.IS_PLAYER_TURN] = True
                                if target_team.prompt_submitted:
                                    response[JSONFields.PROMPT_STATUS] = GamePlay.PROMPTED
                                else:
                                    response[JSONFields.PROMPT_STATUS] = GamePlay.NOT_PROMPTED
                            else:
                                response[JSONFields.IS_PLAYER_TURN] = False
                                response[JSONFields.PROMPT_STATUS] = GamePlay.IMAGE_IN
                        

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