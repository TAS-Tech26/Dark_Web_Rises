import time
from enumerations import JSONFields, Login, NameStatus, Responses, GameState, TeamState, GamePlay
from models.users import Player,Admin
from models.team import Team, WAIT_YOUR_TURN_IMAGE
import asyncio

class GameServer:
    def __init__(self, server_id, max_teams, max_members_per_team, team_names, player_data, admin_data):
        self.server_id = server_id
        self.game_state = GameState.LOGIN_PERIOD
        self.max_members_per_team = max_members_per_team

        self.connected_players = set()
        self.connected_admins = set()
        self.connected_teams = [[] for _ in range(max_teams)]
        self.connected_sockets = {}

        self.countdown_end_time = 0.0
        self.scores = {}
        self.teams = []

        for i in range(max_teams):
            new_team = Team(id=i, max_members=max_members_per_team)
            new_team.team_name = team_names[i]
            self.teams.append(new_team)

        # Swapped to dictionaries for direct O(1) ID lookups
        self.players = {}
        self.admins = {}

        for player_id, info in player_data.items():
            name = info[0]
            pwd = info[1]
            team_id = info[2]
            self.teams[team_id].members.append(player_id)
            new_player = Player(username=name, id=player_id, password=pwd)
            new_player.team_id = team_id
            self.players[player_id] = new_player

        # FIX: Now reading correctly from admin_data!
        for admin_id, info in admin_data.items():
            new_admin = Admin(username=info[0], id=admin_id, password=info[1])
            self.admins[admin_id] = new_admin

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

        for admin in self.admins.values():
            if username == admin.username:
                if pwd == admin.password:
                    uid = admin.id
                    response = {
                        JSONFields.TYPE: Responses.ADMIN_RESPONSE,
                        JSONFields.STATUS: NameStatus.AVAILABLE,
                        JSONFields.AUTHORISED: Login.ACCEPTED,
                        JSONFields.ADMIN_ID: uid,
                    }
                    self.connected_sockets[uid] = socket
                    self.connected_admins.add(uid)
                    return response
                else:
                    response = {
                        JSONFields.TYPE: Responses.ADMIN_RESPONSE,
                        JSONFields.STATUS: NameStatus.AVAILABLE,
                        JSONFields.AUTHORISED: Login.DENIED,
                        JSONFields.ADMIN_ID: None,
                    }
                    return response
        
        for player in self.players.values():
            if username == player.username:
                response[JSONFields.STATUS] = NameStatus.AVAILABLE

                if player.password == pwd:
                    uid = player.id
                    tid = player.team_id
                    target_team = self.teams[tid]

                    if len(self.connected_teams[tid]) < 4 and uid not in self.connected_players:
                        response[JSONFields.AUTHORISED] = Login.ACCEPTED
                        response[JSONFields.USER_ID] = uid
                        response[JSONFields.TEAM_STATE] = target_team.team_state
                        
                        if target_team.team_state == TeamState.PLAYING:
                            is_active_player = (target_team.current_turn_uid == uid)
                            resolved_image = target_team.current_image if is_active_player else WAIT_YOUR_TURN_IMAGE
                            response[JSONFields.IMAGE] = resolved_image
                            response[JSONFields.TIME] = max(0.0, target_team.turn_end_time - time.time())
                            response[JSONFields.TEAM_STATE] = target_team.team_state

                            if is_active_player:
                                response[JSONFields.IS_PLAYER_TURN] = True
                                response[JSONFields.PROMPT_STATUS] = GamePlay.PROMPTED if target_team.prompt_submitted else GamePlay.NOT_PROMPTED
                            else:
                                response[JSONFields.IS_PLAYER_TURN] = False
                                response[JSONFields.PROMPT_STATUS] = GamePlay.IMAGE_IN
                        
                        if target_team.team_state == TeamState.DONE:
                            response[JSONFields.TEAM_SCORE] = target_team.score
                            response[JSONFields.TEAM_RANK] = target_team.rank

                        if self.game_state == GameState.COUNTDOWN:
                            response[JSONFields.TIME] = max(0, self.countdown_end_time - time.time())
                        
                        if self.game_state == GameState.GAME_OVER:
                            response[JSONFields.TOP3] = self.rank_teams()

                        self.connected_players.add(uid)
                        self.connected_sockets[uid] = socket
                        self.teams[tid].connected_sockets[uid] = socket 

                        if uid not in self.connected_teams[tid]:
                            self.connected_teams[tid].append(uid)
                        response[JSONFields.CONNECTED_TEAM_MEMBERS] = len(self.connected_teams[tid])
                break

        return response
    
    def check_logout(self, id, client_type):
        response = {
            JSONFields.TYPE: Responses.LOGOUT_RESPONSE,
            JSONFields.STATUS: NameStatus.DNE,
            JSONFields.AUTHORISED: Login.DENIED
        }
        if client_type == "admin":
            if id in self.connected_admins:
                response[JSONFields.AUTHORISED] = Login.ACCEPTED
                response[JSONFields.STATUS] = NameStatus.AVAILABLE
                self.connected_sockets.pop(id, None)
                self.connected_admins.discard(id)
        else:
            if id in self.connected_players:
                response[JSONFields.AUTHORISED] = Login.ACCEPTED
                response[JSONFields.STATUS] = NameStatus.AVAILABLE

                self.connected_players.remove(id)
                self.connected_sockets.pop(id, None)

                # Safe dictionary lookup instead of buggy list index tracking
                if id in self.players:
                    team_id = self.players[id].team_id
                    if id in self.connected_teams[team_id]:
                        self.connected_teams[team_id].remove(id)
                    if team_id < len(self.teams):
                        self.teams[team_id].connected_sockets.pop(id, None)
        return response
            
    async def announce_to_players(self, payload):
        tasks = []
        for member_id, socket in list(self.connected_sockets.items()):
            # Safe fall-through if it's an admin socket broadcasting global events
            if member_id in self.players:
                team_id = self.players[member_id].team_id
                target_team = self.teams[team_id]
                tasks.append(target_team.safe_send(member_id, socket, payload))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    def rank_teams(self):
        sorted_scores = sorted(self.scores.items(), key=lambda x: x[1], reverse=True)
        top3 = sorted_scores[:3]

        for i, (tid, score) in enumerate(sorted_scores):
           self.teams[tid].rank = i 

        return top3