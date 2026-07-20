class Player:
    def __init__(self, username, id, password):
        self.username = username
        self.id = id
        self.team_id = None
        self.password = password
class Admin:
    def __init__(self,username, id, password):
        self.username = username
        self.id = id
        self.password = password