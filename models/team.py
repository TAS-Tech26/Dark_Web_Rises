import asyncio
import logging
import time

from enumerations import JSONFields, Responses, TeamState, GamePlay
from services.ai_handling import get_image, compare_image, classify_prompt, clamp_score

logger = logging.getLogger("dwr.team")

WAIT_YOUR_TURN_IMAGE = (
    "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='1024' height='1024' viewBox='0 0 1024 1024'>"
    "<rect width='100%' height='100%' fill='%23121214'/>"
    "<text x='50%' y='50%' font-family='monospace' font-size='32' fill='%239ca3af' text-anchor='middle' dominant-baseline='middle'>"
    "WAITING FOR ACTIVE PLAYER TO PROMPT..."
    "</text></svg>"
)

# Base-line penalty applied to a round. A round's total penalty is made up of
# two independent parts (see run_round):
#   1. This base, plus a flat "structural handicap" of `penalty` points per
#      member the team is short of a full roster -- applied every round,
#      regardless of how the round otherwise goes, to offset the fact that
#      fewer active members means fewer prompt->image hops and therefore a
#      final image that drifts less from the original (an inflated
#      similarity score that has nothing to do with skill).
#   2. Per-turn failure penalties (timeouts, disconnects, exhausted invalid
#      prompt attempts), scaled by penalty_multiplier.
BASE_ROUND_PENALTY = 0

# How many invalid prompts a player may submit in a single turn before the
# turn is forfeited. Reset at the start of every turn -- see run_round.
MAX_PROMPT_ATTEMPTS = 3

# How many consecutive failed sends before we treat a socket as genuinely
# dead. A single slow send is a congested network, not a disconnect.
MAX_CONSECUTIVE_SEND_FAILURES = 3

SEND_TIMEOUT_SECONDS = 1.5


class Team:
    def __init__(self, id, max_members):
        self.team_state = TeamState.WAITING
        self.max_members = max_members
        self.id = id
        self.members = []
        self.connected_sockets = {}
        self.team_name = "insert team name here"
        self.score = []
        self.rank = None

        self.input_queue = asyncio.Queue()
        self.current_image = None
        self.original_image = None
        self.current_turn_uid = None
        self.turn_end_time = 0.0
        self.round = 0
        self.rotation_order = []
        self.prompt_submitted = False
        # Invalid-prompt attempts for the *current turn only*.
        self.player_attempts = {}
        # Consecutive send failures per member, used to distinguish a
        # transient slow send from a genuinely dead socket.
        self._send_failures = {}
        # Set by GameServer so that evicting a socket here also clears the
        # server-level connection bookkeeping (see GameServer.__init__).
        self.on_socket_dropped = None

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------
    async def safe_send(self, member_id, socket, payload):
        """Send with a timeout, evicting only genuinely dead sockets.

        This used to delete the socket after a *single* failed or slow send.
        A 1.5s send timeout is common on a congested conference network and
        is not proof of a disconnect -- but the eviction was permanent: the
        member stayed in GameServer.connected_players (so they could not log
        in again) while being absent from Team.connected_sockets (so they
        received no further images and were penalised every remaining turn).
        There was no recovery path short of restarting the process. Now a
        socket is only dropped after several consecutive failures, and the
        drop is propagated to the server so all bookkeeping stays consistent.
        """
        try:
            await asyncio.wait_for(socket.send_json(payload), timeout=SEND_TIMEOUT_SECONDS)
            self._send_failures.pop(member_id, None)
            return True
        except Exception as exc:
            failures = self._send_failures.get(member_id, 0) + 1
            self._send_failures[member_id] = failures
            logger.info(
                "Send to member %s failed (%s/%s consecutive): %s",
                member_id, failures, MAX_CONSECUTIVE_SEND_FAILURES, exc,
            )
            if failures >= MAX_CONSECUTIVE_SEND_FAILURES:
                logger.warning("Dropping member %s after %s consecutive send failures.", member_id, failures)
                self.connected_sockets.pop(member_id, None)
                self._send_failures.pop(member_id, None)
                if self.on_socket_dropped is not None:
                    try:
                        self.on_socket_dropped(member_id, self.id)
                    except Exception:
                        logger.exception("on_socket_dropped hook failed for member %s", member_id)
            return False

    async def send_to(self, member_id, payload):
        """Send to one member if they are currently connected."""
        socket = self.connected_sockets.get(member_id)
        if socket is None:
            return False
        return await self.safe_send(member_id, socket, payload)

    async def announce_to_team(self, payload):
        tasks = [
            self.safe_send(member_id, socket, payload)
            for member_id, socket in list(self.connected_sockets.items())
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # Round loop
    # ------------------------------------------------------------------
    def _drain_queue(self):
        """Discard stale input left over from a previous turn."""
        while not self.input_queue.empty():
            try:
                self.input_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _broadcast_turn(self, active_uid):
        for member_uid, socket in list(self.connected_sockets.items()):
            is_active = member_uid == active_uid
            await self.safe_send(member_uid, socket, {
                JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                JSONFields.MESSAGE: GamePlay.IMAGE_IN,
                JSONFields.IS_PLAYER_TURN: is_active,
                JSONFields.IMAGE: self.current_image if is_active else WAIT_YOUR_TURN_IMAGE,
                JSONFields.TIME: max(0.0, self.turn_end_time - time.time()),
            })

    async def run_round(self, round_num, time_per_round, timeout, penalty):
        """Play one round for this team and return the round score.

        Wrapped so that an unexpected failure costs this team its round score
        but never propagates: run_round() is gathered across every team in
        game.start_games(), so an exception escaping here previously killed
        the *entire event* -- every team's game loop stopped and nobody ever
        received a GAME_OVER frame.
        """
        try:
            return await self._run_round(round_num, time_per_round, timeout, penalty)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Round %s failed for team %s; scoring it 0.", round_num + 1, self.id)
            round_score = clamp_score(0.0)
            self.score.append(round_score)
            return round_score
        finally:
            self.current_turn_uid = None
            self.prompt_submitted = False

    async def _run_round(self, round_num, time_per_round, timeout, penalty):
        # 1. Base rotation on assigned members
        connected_uids = [uid for uid in self.members if uid in self.connected_sockets]

        # Invalid-prompt attempts are per-turn, not per-game. Previously this
        # dict was never cleared between turns or rounds, so a player who
        # used two of their three attempts in round 1 started round 2 with
        # only one attempt left -- while the client unconditionally reset its
        # own "attempts left: 3" display on every new image, so the server
        # and the UI disagreed about the same counter.
        self.player_attempts = {}

        # If nobody is connected at all, mark round 0 immediately
        if not connected_uids:
            round_score = clamp_score(0.0)
            self.score.append(round_score)
            return round_score

        penalty_multiplier = self.max_members / max(1, len(connected_uids))

        # Structural handicap: a short-handed team makes fewer prompt -> image
        # hops over the course of a round, so the final image drifts less from
        # the original and scores an inflated similarity purely from having
        # fewer active members -- not from playing better.
        missing_members = self.max_members - len(connected_uids)
        no_prompt_penalty = BASE_ROUND_PENALTY + (penalty * missing_members)

        self.rotation_order = list(self.members)  # Loop all assigned members
        if round_num != 0 and self.rotation_order:
            shift = round_num % len(self.rotation_order)
            self.rotation_order = self.rotation_order[shift:] + self.rotation_order[:shift]

        logger.info("Team %s round %s rotation order: %s", self.id, round_num + 1, self.rotation_order)
        self.round = round_num
        self.current_image = await get_image("default_prompt")
        self.original_image = self.current_image

        if self.original_image is None:
            # No reference images on disk -- nothing to score against. Fail
            # this round loudly rather than handing out an arbitrary number.
            logger.error("Team %s: no reference image available for round %s.", self.id, round_num + 1)
            round_score = clamp_score(0.0)
            self.score.append(round_score)
            return round_score

        # Number of successful prompt -> image generations this round. If this
        # stays 0 the final image *is* the original reference image, so the
        # similarity score is a perfect 100 and a team that did nothing at all
        # would score ~80/100 after penalties. See the scoring block below.
        successful_hops = 0

        for index, uid in enumerate(self.rotation_order):
            # If every member of the team has gone offline, stop the round
            # rather than sitting through a 10s grace period per remaining
            # player who is never coming back. At ~300 concurrent teams this
            # holds coroutines, queues and timers open far longer than needed.
            if not self.connected_sockets:
                remaining = len(self.rotation_order) - index
                logger.info(
                    "Team %s has no connected players left; ending round %s early (%s turns forfeited).",
                    self.id, round_num + 1, remaining,
                )
                no_prompt_penalty += penalty * penalty_multiplier * remaining
                break

            self.current_turn_uid = uid
            self.turn_end_time = time.time() + time_per_round
            # Fresh attempt budget for this turn.
            self.player_attempts[uid] = 0

            self._drain_queue()

            # --- WINDOW 1: Check connection before turn starts (10s grace period) ---
            if uid not in self.connected_sockets:
                logger.info("Player %s offline at turn start. Waiting 10s grace period to join...", uid)
                wait_start = time.time()
                reconnected = False

                while time.time() - wait_start < 10.0:
                    await asyncio.sleep(0.5)
                    if uid in self.connected_sockets:
                        reconnected = True
                        break

                if not reconnected:
                    logger.info("Player %s failed to connect in 10s. Skipping turn.", uid)
                    no_prompt_penalty += penalty * penalty_multiplier
                    continue

            await self._broadcast_turn(uid)

            disconnect_time = None

            while True:
                # --- WINDOW 2: Mid-turn disconnect handling (20s grace period) ---
                if uid not in self.connected_sockets:
                    if disconnect_time is None:
                        disconnect_time = time.time()
                        logger.info("Player %s disconnected mid-turn! Giving 20s to reconnect...", uid)

                    if time.time() - disconnect_time >= 20.0:
                        logger.info("Player %s failed to reconnect within 20s.", uid)
                        no_prompt_penalty += penalty * penalty_multiplier
                        break
                else:
                    if disconnect_time is not None:
                        logger.info("Player %s reconnected successfully!", uid)
                        disconnect_time = None

                try:
                    time_left = self.turn_end_time - time.time()
                    if time_left <= 0:
                        raise asyncio.TimeoutError()

                    # Poll queue in short chunks so the disconnect timer and
                    # the turn clock stay responsive.
                    data = await asyncio.wait_for(self.input_queue.get(), timeout=min(time_left, 1.0))
                except (asyncio.TimeoutError, TimeoutError):
                    if time.time() >= self.turn_end_time:
                        no_prompt_penalty += penalty * penalty_multiplier
                        break
                    continue

                if not isinstance(data, dict):
                    continue

                status = data.get(JSONFields.STATUS)

                if status == GamePlay.PROMPTED:
                    current_prompt = data.get(JSONFields.PROMPT)

                    if classify_prompt(prompt=current_prompt):
                        generated = await get_image(prompt=current_prompt)

                        if generated is None:
                            # Image generation failed after all retries. Keep
                            # the previous image in the chain rather than
                            # replacing it with a placeholder that the next
                            # player would then be asked to describe -- that
                            # used to corrupt the rest of the round for the
                            # whole team over one upstream hiccup.
                            logger.warning(
                                "Team %s: image generation failed for player %s; keeping the previous image.",
                                self.id, uid,
                            )
                            await self.send_to(uid, {
                                JSONFields.TYPE: Responses.ERROR_RESPONSE,
                                JSONFields.MESSAGE: "Image generation is unavailable right now. Your prompt was accepted.",
                            })
                        else:
                            self.current_image = generated
                            successful_hops += 1

                        self.prompt_submitted = True
                        await self.send_to(uid, {
                            JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                            JSONFields.PROMPT_STATUS: True,
                            JSONFields.MESSAGE: GamePlay.RECEIVED,
                        })
                        break

                    self.player_attempts[uid] = self.player_attempts.get(uid, 0) + 1
                    current_attempts = self.player_attempts[uid]

                    if current_attempts >= MAX_PROMPT_ATTEMPTS:
                        await self.send_to(uid, {
                            JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                            JSONFields.PROMPT_STATUS: False,
                            JSONFields.MESSAGE: GamePlay.OUT_OF_CHANCES,
                            JSONFields.ATTEMPTS_LEFT: 0,
                        })
                        no_prompt_penalty += penalty * penalty_multiplier
                        break

                    await self.send_to(uid, {
                        JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                        JSONFields.PROMPT_STATUS: False,
                        JSONFields.MESSAGE: GamePlay.INVALID_PROMPT,
                        JSONFields.ATTEMPTS_LEFT: MAX_PROMPT_ATTEMPTS - current_attempts,
                    })

                elif status == GamePlay.NOT_PROMPTED:
                    await self.send_to(uid, {
                        JSONFields.TYPE: Responses.GAMEPLAY_RESPONSE,
                        JSONFields.MESSAGE: GamePlay.NOT_RECEIVED,
                    })
                    no_prompt_penalty += penalty * penalty_multiplier
                    break

            self.prompt_submitted = False

        self.current_turn_uid = None

        if successful_hops == 0:
            # The final image is byte-for-byte the reference image, because no
            # prompt ever produced a new one. Scoring that with CLIP returns a
            # perfect similarity, so a team that idled through every turn used
            # to be awarded ~80/100 (100 minus its timeout penalties) -- more
            # than many teams that actually played. No hops means no game was
            # played, so the round is worth 0.
            logger.info(
                "Team %s made no successful prompts in round %s; scoring 0.",
                self.id, round_num + 1,
            )
            round_score = clamp_score(0.0)
        else:
            round_score = await compare_image(no_prompt_penalty, self.original_image, self.current_image)

        self.score.append(round_score)
        return round_score
