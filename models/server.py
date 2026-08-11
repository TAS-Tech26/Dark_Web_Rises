import asyncio
import logging
import secrets
import time

from enumerations import JSONFields, Login, NameStatus, Responses, GameState, TeamState, GamePlay
from models.users import Player, Admin
from models.team import Team, WAIT_YOUR_TURN_IMAGE

logger = logging.getLogger("dwr.server")

# --- Brute-force protection tuning ---
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 60.0

# Admin session tokens expire even if the socket never closes cleanly.
ADMIN_TOKEN_TTL_SECONDS = 12 * 60 * 60


def normalise_username(raw):
    """Usernames are matched case-insensitively and whitespace-trimmed.

    Event attendees type their credentials off a printed card on a phone
    keyboard that auto-capitalises the first letter and appends a trailing
    space. Exact-match lookup turned both into "invalid credentials", which
    also burned one of the five brute-force attempts each time.
    """
    if not isinstance(raw, str):
        return None
    return raw.strip().casefold()


class GameServer:
    def __init__(self, server_id, max_teams, max_members_per_team, team_names, player_data, admin_data):
        self.server_id = server_id
        self.game_state = GameState.LOGIN_PERIOD
        self.max_members_per_team = max_members_per_team
        self.max_teams = max_teams

        self.connected_players = set()
        self.connected_admins = set()
        self.connected_teams = [[] for _ in range(max_teams)]
        self.connected_sockets = {}
        self.current_round = 0
        self.total_rounds = 5

        self.countdown_end_time = 0.0
        self.scores = {}
        self.teams = []
        # Team ids that took part in the game at all. Teams that never
        # connected are excluded from the leaderboard rather than being
        # ranked with a -1 sentinel score.
        self.participating_team_ids = set()

        # Serialises the mutating section of check_login. Password
        # verification now happens off the event loop (see check_login), so
        # two logins can interleave at that await point; the lock keeps the
        # connected_* bookkeeping consistent.
        self._login_lock = asyncio.Lock()

        # --- Startup config validation (fail fast, not mid-game) ---
        if max_teams <= 0:
            raise ValueError("GameServer misconfigured: max_teams must be at least 1.")
        if max_members_per_team <= 0:
            raise ValueError("GameServer misconfigured: max_members_per_team must be at least 1.")
        if len(team_names) < max_teams:
            raise ValueError(
                f"GameServer misconfigured: {max_teams} teams requested but only "
                f"{len(team_names)} team names were supplied."
            )

        for i in range(max_teams):
            new_team = Team(id=i, max_members=max_members_per_team)
            new_team.team_name = team_names[i]
            # A socket dropped inside Team.safe_send must also be cleared from
            # the server-level bookkeeping, otherwise the player stays in
            # connected_players forever (blocking re-login) while being absent
            # from the team's socket map (so they receive nothing).
            new_team.on_socket_dropped = self._on_team_socket_dropped
            self.teams.append(new_team)

        # Swapped to dictionaries for direct O(1) ID lookups
        self.players = {}
        self.admins = {}
        # Secondary indexes so login doesn't have to scan every account on
        # every attempt. At ~1200 concurrent players this turns a per-login
        # O(n) scan into an O(1) dict lookup. Keyed by normalised username.
        self._username_to_player_id = {}
        self._username_to_admin_id = {}

        for player_id, info in player_data.items():
            if len(info) < 3:
                raise ValueError(
                    f"GameServer misconfigured: roster entry for player {player_id!r} must be "
                    f"[username, password, team_id]."
                )
            name, pwd, team_id = info[0], info[1], info[2]
            key = normalise_username(name)
            if not key:
                raise ValueError(f"GameServer misconfigured: player {player_id!r} has an empty username.")
            if not (0 <= team_id < max_teams):
                raise ValueError(
                    f"GameServer misconfigured: player {player_id!r} assigned to "
                    f"team_id {team_id}, which is outside range(0, {max_teams})."
                )
            if len(self.teams[team_id].members) >= max_members_per_team:
                raise ValueError(
                    f"GameServer misconfigured: team {team_id} would exceed its "
                    f"capacity of {max_members_per_team} members."
                )
            if key in self._username_to_player_id:
                raise ValueError(f"GameServer misconfigured: duplicate username {name!r}.")

            self.teams[team_id].members.append(player_id)
            new_player = Player(username=name, id=player_id, password=pwd)
            new_player.team_id = team_id
            self.players[player_id] = new_player
            self._username_to_player_id[key] = player_id

        for admin_id, info in admin_data.items():
            key = normalise_username(info[0])
            if not key:
                raise ValueError(f"GameServer misconfigured: admin {admin_id!r} has an empty username.")
            if key in self._username_to_admin_id or key in self._username_to_player_id:
                raise ValueError(f"GameServer misconfigured: duplicate username {info[0]!r}.")
            new_admin = Admin(username=info[0], id=admin_id, password=info[1])
            self.admins[admin_id] = new_admin
            self._username_to_admin_id[key] = admin_id

        # token -> {"admin_id": int, "expires_at": float}. Random opaque
        # tokens replace the old scheme where HTTP admin endpoints trusted a
        # client-supplied X-Admin-Id integer: since admin ids are small
        # guessable numbers (0, 1, ...), anyone could set that header and hit
        # /admin/rungame without ever knowing an admin password.
        self.admin_tokens = {}

        # Brute-force protection state, keyed by normalised username.
        self._login_attempts = {}

    # ------------------------------------------------------------------
    # Connection bookkeeping
    # ------------------------------------------------------------------
    def _on_team_socket_dropped(self, member_id, team_id):
        """Called by Team.safe_send when a socket is confirmed dead."""
        self.connected_players.discard(member_id)
        self.connected_sockets.pop(member_id, None)
        if 0 <= team_id < len(self.connected_teams) and member_id in self.connected_teams[team_id]:
            self.connected_teams[team_id].remove(member_id)

    # ------------------------------------------------------------------
    # Brute-force helpers
    # ------------------------------------------------------------------
    def _lockout_remaining(self, key):
        """Seconds left on an active lockout, or 0.0 if not locked out.

        Also resets the failure counter once a lockout has expired. Without
        that reset the count stayed at or above MAX_FAILED_ATTEMPTS forever,
        so after the first lockout expired a single further typo re-locked
        the account instantly -- an attendee who fat-fingered their password
        once was effectively locked out for the rest of the event.
        """
        entry = self._login_attempts.get(key)
        if not entry:
            return 0.0
        remaining = entry["locked_until"] - time.time()
        if remaining > 0:
            return remaining
        if entry["count"] >= MAX_FAILED_ATTEMPTS:
            entry["count"] = 0
            entry["locked_until"] = 0.0
        return 0.0

    def _record_failed_login(self, key):
        entry = self._login_attempts.setdefault(key, {"count": 0, "locked_until": 0.0})
        entry["count"] += 1
        if entry["count"] >= MAX_FAILED_ATTEMPTS:
            entry["locked_until"] = time.time() + LOCKOUT_SECONDS
            logger.warning("Account %r locked out for %ss after repeated failed logins.", key, LOCKOUT_SECONDS)

    def _clear_failed_logins(self, key):
        self._login_attempts.pop(key, None)

    # ------------------------------------------------------------------
    # Admin session tokens
    # ------------------------------------------------------------------
    def issue_admin_token(self, admin_id):
        token = secrets.token_urlsafe(32)
        self.admin_tokens[token] = {
            "admin_id": admin_id,
            "expires_at": time.time() + ADMIN_TOKEN_TTL_SECONDS,
        }
        return token

    def resolve_admin_token(self, token):
        """Return the admin id for a live token, or None."""
        if not token:
            return None
        entry = self.admin_tokens.get(token)
        if entry is None:
            return None
        if entry["expires_at"] <= time.time():
            self.admin_tokens.pop(token, None)
            return None
        if entry["admin_id"] not in self.connected_admins:
            return None
        return entry["admin_id"]

    def revoke_admin_tokens_for(self, admin_id):
        for token in [t for t, entry in self.admin_tokens.items() if entry["admin_id"] == admin_id]:
            self.admin_tokens.pop(token, None)

    # ------------------------------------------------------------------
    # Login / logout
    # ------------------------------------------------------------------
    def _denied_response(self, status=NameStatus.DNE, authorised=Login.DENIED, retry_after=None):
        response = {
            JSONFields.TYPE: Responses.LOGIN_RESPONSE,
            JSONFields.STATUS: status,
            JSONFields.AUTHORISED: authorised,
            JSONFields.CONNECTED_TEAM_MEMBERS: None,
            JSONFields.USER_ID: None,
            JSONFields.TEAM_STATE: None,
            JSONFields.IS_PLAYER_TURN: False,
            JSONFields.TIME: None,
            JSONFields.IMAGE: None,
            JSONFields.GAME_STATE: self.game_state,
        }
        if retry_after is not None:
            response[JSONFields.RETRY_AFTER] = round(retry_after, 1)
        return response

    async def check_login(self, data, socket):
        """Authenticate a login frame and register the connection.

        Async because password verification is deliberately expensive
        (PBKDF2, 200k iterations ~= 56 ms of CPU on a typical App Service
        core) and must not run on the event loop. With ~1200 attendees all
        logging in within a couple of minutes, doing that inline blocks the
        single event loop for roughly 68 seconds in aggregate -- during which
        no websocket frame for any other player is processed. It also gave
        anyone with an open socket a trivial CPU-exhaustion DoS: spam login
        frames and the whole event stalls. Verification now runs on a worker
        thread; the state mutation afterwards is serialised by _login_lock.
        """
        response = self._denied_response()

        username = data.get(JSONFields.USERNAME)
        pwd = data.get(JSONFields.PASSWORD)

        # Basic input validation: reject anything that isn't a reasonably
        # sized string before it touches account lookups.
        if not isinstance(username, str) or not isinstance(pwd, str):
            return response
        if not (0 < len(username) <= 64) or not (0 < len(pwd) <= 256):
            return response

        key = normalise_username(username)
        if not key:
            return response

        remaining = self._lockout_remaining(key)
        if remaining > 0:
            logger.warning("Login attempt for locked-out account %r rejected.", key)
            # Reported distinctly so the UI can say "locked, try again in Ns"
            # instead of "wrong password" -- attendees otherwise keep
            # retrying, extending the lockout.
            #
            # The frame type must match the *account* type. A locked-out admin
            # used to receive a login_response, which the admin client ignores
            # entirely (it only listens for admin_response) -- so the screen
            # showed nothing at all and the lockout was invisible.
            if key in self._username_to_admin_id:
                return {
                    JSONFields.TYPE: Responses.ADMIN_RESPONSE,
                    JSONFields.STATUS: NameStatus.LOCKED,
                    JSONFields.AUTHORISED: Login.LOCKED,
                    JSONFields.ADMIN_ID: None,
                    JSONFields.RETRY_AFTER: round(remaining, 1),
                }
            return self._denied_response(
                status=NameStatus.LOCKED, authorised=Login.LOCKED, retry_after=remaining
            )

        admin_id = self._username_to_admin_id.get(key)
        if admin_id is not None:
            admin = self.admins[admin_id]
            verified = await asyncio.to_thread(admin.verify_password, pwd)
            if not verified:
                self._record_failed_login(key)
                return {
                    JSONFields.TYPE: Responses.ADMIN_RESPONSE,
                    JSONFields.STATUS: NameStatus.AVAILABLE,
                    JSONFields.AUTHORISED: Login.DENIED,
                    JSONFields.ADMIN_ID: None,
                }

            async with self._login_lock:
                self._clear_failed_logins(key)
                token = self.issue_admin_token(admin_id)
                self.connected_sockets[admin_id] = socket
                self.connected_admins.add(admin_id)
            return {
                JSONFields.TYPE: Responses.ADMIN_RESPONSE,
                JSONFields.STATUS: NameStatus.AVAILABLE,
                JSONFields.AUTHORISED: Login.ACCEPTED,
                JSONFields.ADMIN_ID: admin_id,
                JSONFields.ADMIN_TOKEN: token,
            }

        player_id = self._username_to_player_id.get(key)
        if player_id is None:
            return response

        player = self.players[player_id]
        verified = await asyncio.to_thread(player.verify_password, pwd)
        response[JSONFields.STATUS] = NameStatus.AVAILABLE

        if not verified:
            self._record_failed_login(key)
            return response

        async with self._login_lock:
            uid = player.id
            tid = player.team_id
            target_team = self.teams[tid]

            # Reconnect handling. The old check was
            # `uid not in self.connected_players` -> deny, which made
            # reconnection impossible: the frontend reconnects and re-sends
            # credentials the moment a socket drops, but the server only
            # clears connected_players when it notices the *old* socket
            # closed. On an abrupt network drop (phone leaving wifi, laptop
            # sleeping) TCP can take tens of seconds to surface that, so the
            # player's own re-login was rejected as "already connected" and
            # they sat out the rest of the event. A correct password is
            # sufficient proof of identity, so the new session simply
            # replaces the old one.
            previous_socket = self.connected_sockets.get(uid)
            if previous_socket is not None and previous_socket is not socket:
                logger.info("Player %s re-authenticated; replacing the previous session.", uid)
                target_team.connected_sockets.pop(uid, None)

            if uid not in self.connected_players and len(self.connected_teams[tid]) >= target_team.max_members:
                # Genuinely full team (more distinct accounts connected than
                # the team has seats) -- this is a roster misconfiguration.
                logger.warning("Team %s is full; rejecting login for player %s.", tid, uid)
                return response

            self._clear_failed_logins(key)
            response[JSONFields.AUTHORISED] = Login.ACCEPTED
            response[JSONFields.USER_ID] = uid
            response[JSONFields.TEAM_ID] = tid
            response[JSONFields.TEAM_NAME] = target_team.team_name
            response[JSONFields.TEAM_STATE] = target_team.team_state
            response[JSONFields.TOTAL_ROUNDS] = self.total_rounds
            response[JSONFields.ROUND] = self.current_round

            if target_team.team_state == TeamState.PLAYING:
                is_active_player = (target_team.current_turn_uid == uid)
                response[JSONFields.IMAGE] = (
                    target_team.current_image if is_active_player else WAIT_YOUR_TURN_IMAGE
                )
                response[JSONFields.TIME] = max(0.0, target_team.turn_end_time - time.time())
                response[JSONFields.IS_PLAYER_TURN] = is_active_player
                if is_active_player:
                    response[JSONFields.PROMPT_STATUS] = (
                        GamePlay.PROMPTED if target_team.prompt_submitted else GamePlay.NOT_PROMPTED
                    )
                    response[JSONFields.ATTEMPTS_LEFT] = max(
                        0, 3 - target_team.player_attempts.get(uid, 0)
                    )
                else:
                    response[JSONFields.PROMPT_STATUS] = GamePlay.IMAGE_IN

            if target_team.team_state == TeamState.DONE:
                # Always a number, with the per-round list under its own key.
                response[JSONFields.TEAM_SCORE] = self.team_total(target_team)
                response[JSONFields.ROUND_SCORES] = list(target_team.score)
                response[JSONFields.TEAM_RANK] = target_team.rank

            if self.game_state == GameState.COUNTDOWN:
                response[JSONFields.TIME] = max(0.0, self.countdown_end_time - time.time())

            if self.game_state == GameState.GAME_OVER:
                response[JSONFields.TOP3] = self.rank_teams()
                response[JSONFields.LEADERBOARD] = self.leaderboard()

            self.connected_players.add(uid)
            self.connected_sockets[uid] = socket
            target_team.connected_sockets[uid] = socket

            if uid not in self.connected_teams[tid]:
                self.connected_teams[tid].append(uid)
            response[JSONFields.CONNECTED_TEAM_MEMBERS] = len(self.connected_teams[tid])

        return response

    def check_logout(self, id, client_type, token=None):
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

                if token is not None:
                    self.admin_tokens.pop(token, None)
                else:
                    # Fallback: revoke every token issued to this admin id
                    # (e.g. socket dropped before we could track the token).
                    self.revoke_admin_tokens_for(id)
        else:
            if id in self.connected_players:
                response[JSONFields.AUTHORISED] = Login.ACCEPTED
                response[JSONFields.STATUS] = NameStatus.AVAILABLE

                self.connected_players.discard(id)
                self.connected_sockets.pop(id, None)

                # Safe dictionary lookup instead of buggy list index tracking
                if id in self.players:
                    team_id = self.players[id].team_id
                    if team_id is not None and 0 <= team_id < len(self.teams):
                        if id in self.connected_teams[team_id]:
                            self.connected_teams[team_id].remove(id)
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

    # ------------------------------------------------------------------
    # Scoring / ranking
    # ------------------------------------------------------------------
    @staticmethod
    def team_total(team):
        """A team's cumulative score as a single number.

        `team.score` is a list of per-round scores; several call sites used to
        send the raw list under the same `team_score` key that other frames
        used for an integer total, forcing clients to type-sniff.
        """
        if isinstance(team.score, list):
            return round(sum(s for s in team.score if isinstance(s, (int, float))), 2)
        return round(team.score or 0, 2)

    def leaderboard(self):
        """Full ranked leaderboard of teams that actually took part.

        Ranks are 1-based and tie-aware (two teams on the same score share a
        rank). Teams that never connected are excluded entirely rather than
        being ranked with the -1 sentinel -- they used to occupy places in
        `top3` when fewer than three teams turned up, so the results screen
        showed phantom teams on -1 points.
        """
        entries = [
            {
                "team_id": team.id,
                "team_name": team.team_name,
                "score": self.team_total(team),
            }
            for team in self.teams
            if team.id in self.participating_team_ids
        ]
        entries.sort(key=lambda e: (-e["score"], e["team_id"]))

        rank = 0
        previous_score = None
        for position, entry in enumerate(entries, start=1):
            if entry["score"] != previous_score:
                rank = position
                previous_score = entry["score"]
            entry["rank"] = rank
            self.teams[entry["team_id"]].rank = rank

        return entries

    def rank_teams(self):
        """Top three (team_id, score) pairs. Kept for wire compatibility."""
        entries = self.leaderboard()
        return [(entry["team_id"], entry["score"]) for entry in entries[:3]]
