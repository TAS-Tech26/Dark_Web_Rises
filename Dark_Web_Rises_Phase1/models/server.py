import asyncio
import logging
import os
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

# How long a session gets to answer a liveness probe before the account is
# considered abandoned and released to a new login.
#
# The trade-off is between two failure modes, and they are not symmetric.
# Too SHORT and a player on a slow connection loses their session to anyone
# with their credentials. Too LONG and a player whose phone died waits that
# long, staring at a refusal, before they can sign in on a borrowed laptop --
# and they will spend the wait at the help desk.
#
# 3s is comfortably above any round-trip a browser on venue wifi should need
# to answer a one-word frame, and short enough that a genuine switch of
# devices feels like a pause rather than a fault.
SESSION_PROBE_TIMEOUT = float(os.getenv("SESSION_PROBE_TIMEOUT", "3.0"))


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

        # Phase 2 handoff, retained after game over so a RECONNECTING team can
        # still be told what it qualified for.
        #
        # These used to be local to _finish_game: computed, broadcast once, and
        # discarded. Any player whose phone blinked during the game-over frame
        # then reached the results screen with qualified=None, and the block
        # that says "Write this down now -- it is shown once, on this screen
        # only" never rendered. That is a silent, unrecoverable loss of the
        # Phase 2 handoff for that team, since CTFd stores the password hashed.
        self.phase2_qualified_ids = set()
        self.phase2_passwords = {}
        self.phase2_url = None

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

        # Admins and players share self.connected_sockets, so a shared id is a
        # socket collision, not a name collision: the admin login overwrites the
        # player's entry, that player silently stops receiving broadcasts, and on
        # reconnect the liveness probe is answered by the admin's dashboard --
        # locking the player out of their own account for the whole event.
        # roster.ADMIN_ID_BASE keeps the ranges apart; this asserts it stayed
        # that way, because the symptom otherwise looks like a frontend bug.
        overlap = set(self.admins) & set(self.players)
        if overlap:
            raise ValueError(
                f"GameServer misconfigured: id(s) {sorted(overlap)} are used by both "
                f"an admin and a player. Admin ids must not overlap roster ids -- "
                f"see roster.ADMIN_ID_BASE."
            )

        # token -> {"admin_id": int, "expires_at": float}. Random opaque
        # tokens replace the old scheme where HTTP admin endpoints trusted a
        # client-supplied X-Admin-Id integer: since admin ids are small
        # guessable numbers (0, 1, ...), anyone could set that header and hit
        # /admin/rungame without ever knowing an admin password.
        self.admin_tokens = {}

        # Brute-force protection state, keyed by normalised username.
        self._login_attempts = {}

        # uid -> asyncio.Event, set when that player's current session answers
        # a liveness probe. See _incumbent_is_alive.
        self._session_probes = {}

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

        # --- One account, one device --------------------------------------
        # The password is right. Before letting this connection take the
        # account, find out whether somebody is already using it.
        #
        # Deliberately OUTSIDE _login_lock. The probe waits up to
        # SESSION_PROBE_TIMEOUT for an answer, and that lock serialises every
        # login on the server -- holding it while waiting on one player's
        # phone would stall the whole room's sign-in behind them.
        incumbent = self.connected_sockets.get(player.id)
        if incumbent is not None and incumbent is not socket:
            if await self._incumbent_is_alive(player.id, incumbent):
                logger.info(
                    "Refused a second login for player %s: the existing session "
                    "answered a liveness probe.", player.id,
                )
                return self._already_connected_response()
            # It did not answer, so it is gone. Release the account.
            #
            # connected_sockets must be cleared here, not just the team map.
            # The re-check inside the lock below asks "is anyone registered on
            # this account?" -- and if the corpse is still in connected_sockets
            # it answers yes and refuses the very login this branch just
            # decided to allow. The player would be told the account is in use
            # on another device by the same code that had, a millisecond
            # earlier, concluded that device no longer exists.
            logger.info(
                "Player %s: the previous session is unresponsive; releasing the "
                "account to this connection.", player.id,
            )
            self.connected_sockets.pop(player.id, None)
            self.connected_players.discard(player.id)
            self.teams[player.team_id].connected_sockets.pop(player.id, None)
            asyncio.create_task(self._retire_socket(incumbent, player.id))

        async with self._login_lock:
            # Re-checked inside the lock. Two devices can race here -- both
            # find no incumbent, both probe, both proceed -- and without this
            # the later one would silently take an account the earlier one had
            # just claimed. Cheap to check, and it closes the window.
            incumbent = self.connected_sockets.get(player.id)
            if incumbent is not None and incumbent is not socket:
                logger.info(
                    "Refused a second login for player %s: another connection "
                    "claimed the account while this one was being checked.",
                    player.id,
                )
                return self._already_connected_response()
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
            # Any incumbent has already been dealt with above: either it
            # answered a probe and this login was refused, or it did not and
            # was retired. Reaching here means the account is free.

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
            # TeamState.DONE is overloaded: it means BOTH "the event has
            # finished" and "this team had nobody connected when the game
            # started". Those need opposite answers here.
            #
            # Reporting DONE verbatim during a live game sent a late arrival
            # straight to the final-standings screen -- mid-event, with a score
            # of 0, while everyone else was still playing. They are not
            # finished; they are waiting to be admitted at the next round
            # boundary (see _play_all_rounds' late-admission pass).
            #
            # WAITING is the honest answer, and it routes them to the lobby.
            awaiting_admission = (
                target_team.team_state == TeamState.DONE
                and self.game_state != GameState.GAME_OVER
            )
            response[JSONFields.TEAM_STATE] = (
                TeamState.WAITING if awaiting_admission else target_team.team_state
            )
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

            # Gated on the GAME being over, not on the TEAM being DONE -- see
            # the note above. Scores and the Phase 2 handoff are only meaningful
            # once the event has actually concluded.
            if self.game_state == GameState.GAME_OVER:
                # Always a number, with the per-round list under its own key.
                response[JSONFields.TEAM_SCORE] = self.team_total(target_team)
                response[JSONFields.ROUND_SCORES] = list(target_team.score)
                response[JSONFields.TEAM_RANK] = target_team.rank

                # Phase 2 handoff, so a team that reconnects after game over
                # still gets its CTFd credentials. Only populated once
                # _finish_game has run; before that these are empty and the
                # fields resolve to None/False, which is correct -- a team that
                # is DONE mid-game (never connected at the start) has not
                # qualified for anything.
                is_qualified = target_team.id in self.phase2_qualified_ids
                response[JSONFields.QUALIFIED] = is_qualified
                response[JSONFields.PHASE2_URL] = self.phase2_url if is_qualified else None
                response[JSONFields.PHASE2_USERNAME] = (
                    target_team.team_name if is_qualified else None
                )
                response[JSONFields.PHASE2_PASSWORD] = self.phase2_passwords.get(target_team.id)

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

    def _already_connected_response(self):
        """Refuse a login because the account is in use on a live device.

        A separate status from DENIED on purpose. Told "invalid credentials",
        a player retypes their password -- and after five attempts the
        brute-force lockout fires and they cannot log in ANYWHERE for a
        minute, having done nothing wrong. The message has to name the actual
        problem and the actual fix.
        """
        return self._denied_response(
            status=NameStatus.ALREADY_CONNECTED,
            authorised=Login.ALREADY_CONNECTED,
        ) | {
            JSONFields.MESSAGE: (
                "This account is already signed in on another device. "
                "Log out there first, then sign in here."
            ),
        }

    async def _incumbent_is_alive(self, uid, incumbent) -> bool:
        """Is there still a real browser on `incumbent`, or just a socket?

        Asks, rather than assumes. A registered socket proves only that the
        server has not yet noticed a disconnect, and it can take TCP minutes
        to notice one -- a phone that walks out of wifi range, a laptop lid
        closing, a browser the OS killed to reclaim memory. Refusing a login
        on the strength of a socket that no longer has anyone behind it would
        lock a player out of their own account for that entire window, which
        at 700 attendees is a support queue rather than a policy.

        So the incumbent is sent a probe and given SESSION_PROBE_TIMEOUT to
        answer. The client replies automatically. An answer means somebody is
        genuinely on the other device; silence means the account is free.

        Errs toward *releasing* the account. If the probe cannot be sent, or
        the reply is late, or anything at all goes wrong, this returns False
        and the new login proceeds. The failure that matters here is a player
        who cannot get in; two sessions briefly coexisting is recoverable, a
        locked-out player mid-round is not.
        """
        event = asyncio.Event()
        self._session_probes[uid] = event
        try:
            try:
                await asyncio.wait_for(
                    incumbent.send_json({
                        JSONFields.TYPE: Responses.SESSION_PROBE,
                    }),
                    timeout=SESSION_PROBE_TIMEOUT,
                )
            except Exception:
                # Could not even write to it. Definitively gone.
                return False

            try:
                await asyncio.wait_for(event.wait(), timeout=SESSION_PROBE_TIMEOUT)
            except (asyncio.TimeoutError, TimeoutError):
                logger.info(
                    "Player %s: the existing session did not answer a liveness probe "
                    "within %.1fs; treating it as gone and releasing the account.",
                    uid, SESSION_PROBE_TIMEOUT,
                )
                return False
            return True
        finally:
            # Only clear it if it is still ours -- a second probe for the same
            # player may have replaced it while we were waiting.
            if self._session_probes.get(uid) is event:
                self._session_probes.pop(uid, None)

    def note_session_probe_ack(self, uid):
        """Called by the websocket loop when a client answers a probe."""
        event = self._session_probes.get(uid)
        if event is not None:
            event.set()

    async def _retire_socket(self, socket, uid):
        """Tell a superseded connection why it is going away, then close it.

        Best effort throughout. The usual reason a session is being replaced is
        that the original device is already unreachable -- a phone that left
        wifi, a laptop that slept, a tab that was closed. Failing to deliver
        the explanation to a socket that was never going to receive it must
        not raise, and must not stop the close.
        """
        try:
            await asyncio.wait_for(
                socket.send_json({
                    JSONFields.TYPE: Responses.SESSION_REPLACED,
                    JSONFields.MESSAGE: (
                        "You have been signed out because this account signed in "
                        "on another device."
                    ),
                }),
                timeout=2.0,
            )
        except Exception:
            pass
        try:
            await socket.close(code=4001)
        except Exception:
            pass
        logger.info("Retired the previous session for player %s.", uid)

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
