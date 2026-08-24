"""End-to-end rehearsal of the whole event, at full scale, in one process.

    python tools/e2e_dryrun.py                    # 175 teams, 700 players
    python tools/e2e_dryrun.py --teams 20         # quick smoke run
    python tools/e2e_dryrun.py --keep             # leave the artifacts to inspect

What this covers, in the order the moderator does it
----------------------------------------------------
    1. roster loads and produces the expected team/player counts
    2. every player logs in over a real websocket
    3. the moderator's admin token is issued and /admin/rungame is accepted
    4. all rounds play through, with every team prompting on every turn
    5. game over writes roster_cache_phase2.csv and phase1_scores.json
    6. the CSV is in the shape CTFd's bulk user import actually requires
    7. final_leaderboard.py finds phase1_scores.json from the documented
       working directory and produces a scoreboard

Steps 5-7 are the ones worth having. Each of them is a hand-off between two
systems that are developed and tested separately, and every hand-off here has
already been wrong at least once: the phase 2 CSV path, the scores file the
leaderboard script reads, the qualification cut. Those failures all surface
at the end of the event, in front of everyone, at the point where there is no
time to fix anything.

What is faked, and why that is honest
-------------------------------------
Image generation is replaced with a stub that returns a real PNG after a
configurable delay. Everything else -- the websocket protocol, the login path,
GameServer, Team.run_round, the round clock, checkpointing, the phase 2
handoff -- is the shipping code.

Faking the provider is deliberate, not a shortcut. This run makes 3500 image
requests at full scale; sending those to DeepInfra would cost real money to
test code paths that have nothing to do with DeepInfra. The provider is
covered properly and separately by tools/loadtest_deepinfra.py, which is the
right instrument for that question. Mixing the two would make a slow, expensive
test that answers neither question well.

CLIP scoring is also stubbed by default (--real-scoring turns it on). Loading
RN50 and running 3500 encodes turns a two-minute rehearsal into an hour, and
the scoring path is deterministic and independently covered. With the stub,
scores are synthetic but *ordered* -- which is what the qualification cut and
the leaderboard actually depend on.

Timings are compressed by default so a full 175-team run takes minutes rather
than the ~31 minutes the real event takes. --realtime uses production timings.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

PASSED, FAILED = [], []


def check(label, condition, detail=""):
    line = f"{label}" + (f"  -- {detail}" if detail else "")
    if condition:
        PASSED.append(line)
        print(f"  [PASS] {line}")
    else:
        FAILED.append(line)
        print(f"  [FAIL] {line}")
    return bool(condition)


def section(title):
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


# ----------------------------------------------------------------------
# Environment
# ----------------------------------------------------------------------
def configure_environment(args, workdir):
    """Set every variable the app reads BEFORE importing it.

    game.py does most of its work at import time -- it loads the roster,
    constructs GameServer and mounts static files as a side effect of being
    imported. So configuration has to be in place first; setting it afterwards
    would silently have no effect, which is its own trap and worth stating.
    """
    roster_path = os.path.join(workdir, "roster_cache.json")
    write_roster(roster_path, args.teams, args.members)

    env = {
        "DWR_STATE_DIR": os.path.join(workdir, "state"),
        "ROSTER_SOURCE": "cache",
        "ROSTER_CACHE_FILE": roster_path,
        "MAX_MEMBERS_PER_TEAM": str(args.members),
        "DWR_ADMINS": "dryrunadmin:dryrun-password",
        "CORS_ALLOWED_ORIGINS": "http://localhost:5173",
        "TOTAL_ROUNDS": str(args.rounds),
        "LOG_LEVEL": args.log_level,
        "PHASE2_CTFD_URL": "http://dryrun.invalid:8080",
    }
    if not args.real_images:
        # Pinned to a provider that is never actually called, because
        # generation is stubbed below. Setting it at all is belt-and-braces:
        # if a future change to install_stubs missed a from-import, this at
        # least means the leaked calls go somewhere free rather than somewhere
        # billed.
        env["IMAGE_PROVIDERS"] = "pollinations"
    if not args.realtime:
        # Compressed clock. The real event's timings are a player-experience
        # decision, not a correctness one -- every code path below behaves
        # identically at 3 seconds a turn and at 90.
        env.update({
            "TIME_PER_ROUND": str(args.turn_seconds),
            "ROUND_BREAK_SECONDS": "0.2",
            "COUNTDOWN_SECONDS": "0.2",
        })
    os.makedirs(env["DWR_STATE_DIR"], exist_ok=True)
    os.environ.update(env)
    return env


def write_roster(path, teams, members):
    """A roster cache in exactly the shape roster.read_cache() expects."""
    players = []
    pid = 0
    for team_id in range(teams):
        for seat in range(members):
            players.append({
                "id": pid,
                "username": f"player{pid:04d}",
                "password": f"pw{pid:04d}",
                "team_id": team_id,
            })
            pid += 1
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {"version": 1, "fetched_at": time.time(),
             "team_count": teams, "players": players},
            fh,
        )
    return players


# ----------------------------------------------------------------------
# Stubs
# ----------------------------------------------------------------------
def fake_compare_image_factory(ai, stats):
    """A stand-in for CLIP scoring, shared by the stubbed and --real-images
    paths so the two cannot drift apart."""

    async def fake_compare_image(penalty, original, new):
        stats["scored"] += 1
        # Deterministic and spread across the full range, so the leaderboard
        # has a real ordering to sort and the top-50% cut has a meaningful
        # boundary to land on. A constant score would make the qualification
        # test pass for the wrong reason.
        base = 40 + (hash((str(original), str(new))) % 6000) / 100.0
        return ai.clamp_score(base - penalty)

    return fake_compare_image


def install_stubs(args):
    """Replace image generation and (optionally) CLIP scoring.

    Patched on the module object that models/team.py imported from, and *also*
    on team.py's own module globals -- team.py does `from services.ai_handling
    import get_image, compare_image`, which binds the names at import time, so
    patching only services.ai_handling would leave team.py calling the
    originals. Getting this wrong produces a test that silently hits the real
    provider, which is the exact failure a dry run must not have.
    """
    import services.ai_handling as ai
    import models.team as team_module

    reference_images = [f"/static/images/ref{i}.png" for i in range(5)]
    stats = {"generated": 0, "scored": 0, "budget_refusals": 0}
    rng = random.Random(args.seed)

    if args.real_images:
        # --- REAL, BILLED GENERATION -------------------------------------
        # Every call from here goes to the configured provider chain and costs
        # money. The hard cap below is not a nicety: this harness drives the
        # real game loop, and a bug in a round loop -- an off-by-one in the
        # rotation, a round that never terminates, a retry that was supposed
        # to be removed -- spends the budget at 175 requests a second with
        # nothing to stop it. The cap is enforced at the single point every
        # generation passes through, so nothing can route around it.
        #
        # Refusing returns None, which is a value models/team.py already
        # handles ("keep the previous image"), so hitting the cap degrades the
        # run instead of crashing it -- you still get a scored game and a
        # report, just with the tail of it ungenerated.
        real_get_image = ai.get_image

        async def budgeted_get_image(prompt):
            if prompt == "default_prompt":
                # Reference images are read off disk, not generated. Free.
                return await real_get_image(prompt)
            if stats["generated"] >= args.max_images:
                stats["budget_refusals"] += 1
                if stats["budget_refusals"] == 1:
                    print(f"\n  !! image budget of {args.max_images} reached; "
                          f"further generations are being refused rather than "
                          f"billed.\n     The run continues -- team.py keeps the "
                          f"previous image when generation returns None.\n")
                return None
            stats["generated"] += 1
            return await real_get_image(prompt)

        ai.get_image = budgeted_get_image
        team_module.get_image = budgeted_get_image

        if not args.real_scoring:
            ai.compare_image = fake_compare_image_factory(ai, stats)
            team_module.compare_image = ai.compare_image
            import game as game_module
            ai.warm_up_model = lambda: None
            game_module.warm_up_model = lambda: None
        return stats

    async def fake_get_image(prompt):
        if prompt == "default_prompt":
            return rng.choice(reference_images)
        stats["generated"] += 1
        if args.image_latency:
            await asyncio.sleep(args.image_latency)
        # A failure rate models the thing that actually happens on the day and
        # exercises team.py's "keep the previous image" branch, which is
        # otherwise never taken in a test.
        if rng.random() < args.image_failure_rate:
            return None
        return f"/static/generated/stub{stats['generated']}.jpg"

    fake_compare_image = fake_compare_image_factory(ai, stats)

    ai.get_image = fake_get_image
    team_module.get_image = fake_get_image
    if not args.real_scoring:
        ai.compare_image = fake_compare_image
        team_module.compare_image = fake_compare_image
        # game.py also does `from services.ai_handling import warm_up_model`,
        # so the name has to be replaced in *its* namespace too -- patching
        # only the source module leaves game.py holding the original and the
        # run downloads ~100MB of CLIP weights it will never use. Same
        # from-import trap as get_image above; worth patching both rather than
        # relying on remembering which modules imported what.
        import game as game_module
        ai.warm_up_model = lambda: None
        game_module.warm_up_model = lambda: None
    return stats


# ----------------------------------------------------------------------
# Simulated clients
# ----------------------------------------------------------------------
class Player:
    """One attendee, over a real websocket, speaking the real protocol."""

    def __init__(self, client, uid, username, password, team_id, enums):
        self.client, self.uid = client, uid
        self.username, self.password, self.team_id = username, password, team_id
        self.E = enums
        self.ws = None
        self.logged_in = False
        self.prompts_sent = 0
        self.saw_game_over = False
        self.game_over_frame = None

    async def run(self, stop_event, prompt_delay):
        JSONFields, Login, Responses, GamePlay, GameState = self.E
        with self.client.websocket_connect("/ws") as ws:
            self.ws = ws
            ws.send_json({
                JSONFields.TYPE: Login.LOGIN,
                JSONFields.USERNAME: self.username,
                JSONFields.PASSWORD: self.password,
            })
            while not stop_event.is_set():
                try:
                    frame = ws.receive_json()
                except Exception:
                    return
                ftype = frame.get(JSONFields.TYPE)

                if ftype == Responses.LOGIN_RESPONSE:
                    self.logged_in = frame.get(JSONFields.AUTHORISED) == Login.ACCEPTED
                    if not self.logged_in:
                        return

                elif ftype == Responses.GAMEPLAY_RESPONSE:
                    if (frame.get(JSONFields.MESSAGE) == GamePlay.IMAGE_IN
                            and frame.get(JSONFields.IS_PLAYER_TURN)):
                        # A prompt that passes classify_prompt(): >= 4 words,
                        # mostly letters, containing a common word.
                        if prompt_delay:
                            await asyncio.sleep(prompt_delay)
                        ws.send_json({
                            JSONFields.TYPE: GamePlay.PROMPT_OUT,
                            JSONFields.STATUS: GamePlay.PROMPTED,
                            JSONFields.PROMPT: (
                                f"a photograph of a red bicycle beside the old "
                                f"stone wall number {self.uid}"
                            ),
                        })
                        self.prompts_sent += 1

                elif ftype == Responses.GAME_STATE_RESPONSE:
                    if frame.get(JSONFields.GAME_STATE) == GameState.GAME_OVER:
                        self.saw_game_over = True
                        self.game_over_frame = frame
                        return


# ----------------------------------------------------------------------
# The run
# ----------------------------------------------------------------------
def run(args):
    workdir = args.workdir or tempfile.mkdtemp(prefix="dwr-e2e-")
    print(f"working directory: {workdir}\n")

    if args.real_images:
        # Stated in units of money before anything is spent. The default run
        # is free, so someone reaching for --real-images has changed the
        # economics of this script and should see that written down rather
        # than inferred.
        planned = args.teams * args.members * args.rounds
        billed = min(planned, args.max_images)
        print("=" * 74)
        print("REAL IMAGE GENERATION IS ON -- this run will be billed")
        print("=" * 74)
        print(f"  provider chain   {os.getenv('IMAGE_PROVIDERS', 'deepinfra,replicate')}")
        print(f"  model            {os.getenv('DEEPINFRA_MODEL', '(default)')}")
        print(f"  size             {os.getenv('DEEPINFRA_SIZE', '512')}x"
              f"{os.getenv('DEEPINFRA_SIZE', '512')}")
        print(f"  game needs       {planned} images "
              f"({args.teams} teams x {args.members} turns x {args.rounds} rounds)")
        print(f"  hard cap         {args.max_images}")
        print(f"  will generate    {billed}")
        if args.cost_per_image:
            print(f"  estimated cost   ${billed * args.cost_per_image:.2f} "
                  f"at ${args.cost_per_image}/image")
        if planned > args.max_images:
            print(f"\n  NOTE: the game wants {planned} images but the cap is "
                  f"{args.max_images}. The last\n        {planned - args.max_images} "
                  f"generations will be refused and those turns will reuse the\n"
                  f"        previous image. Scores stay valid; the tail of the "
                  f"game is just less\n        interesting. Set --teams "
                  f"{args.max_images // (args.members * args.rounds)} for an "
                  f"exact fit.")
        print("=" * 74)
        try:
            reply = input("\nType 'yes' to spend it: ").strip().lower()
        except EOFError:
            reply = "yes"   # non-interactive (CI); the cap is the real guard
        if reply != "yes":
            print("Aborted. Nothing was sent.")
            return 0
        print()

    env = configure_environment(args, workdir)

    section("1. ROSTER AND STARTUP")
    # Imported only now, after the environment is set -- see
    # configure_environment's docstring for why the order is load-bearing.
    import game
    from enumerations import JSONFields, Login, Responses, GamePlay, GameState, TeamState
    from starlette.testclient import TestClient

    stats = install_stubs(args)
    server = game.server

    expected_players = args.teams * args.members
    check("roster produced the expected team count",
          len(server.teams) == args.teams,
          f"{len(server.teams)} teams, expected {args.teams}")
    check("roster produced the expected player count",
          len(server.players) == expected_players,
          f"{len(server.players)} players, expected {expected_players}")
    check("every team is fully staffed",
          all(len(t.members) == args.members for t in server.teams),
          f"team sizes: {sorted({len(t.members) for t in server.teams})}")

    enums = (JSONFields, Login, Responses, GamePlay, GameState)

    with TestClient(game.app) as client:
        assert client.get("/health").status_code == 200

        section("2. PLAYERS LOG IN")
        # Each simulated player runs its socket in a thread, because
        # TestClient's websocket_connect is synchronous. That is the same
        # concurrency shape as real clients from the server's point of view.
        import threading

        stop_event = threading.Event()
        players = []
        for uid, player in server.players.items():
            players.append(Player(
                client, uid, player.username,
                # The roster cache holds the plaintext password; GameServer
                # hashed its copy at construction, so read it from the file.
                f"pw{uid:04d}", player.team_id, enums,
            ))

        threads = []
        loops = []

        def drive(p):
            loop = asyncio.new_event_loop()
            loops.append(loop)
            asyncio.set_event_loop(loop)
            with contextlib.suppress(Exception):
                loop.run_until_complete(p.run(stop_event, args.prompt_delay))
            with contextlib.suppress(Exception):
                loop.close()

        for p in players:
            t = threading.Thread(target=drive, args=(p,), daemon=True)
            t.start()
            threads.append(t)

        deadline = time.time() + args.login_timeout
        while time.time() < deadline:
            if len(server.connected_players) >= expected_players:
                break
            time.sleep(0.2)

        check("every player is connected",
              len(server.connected_players) == expected_players,
              f"{len(server.connected_players)}/{expected_players} connected")
        connected_teams = sum(1 for t in server.teams if t.connected_sockets)
        check("every team has at least one connected player",
              connected_teams == args.teams,
              f"{connected_teams}/{args.teams} teams")

        section("3. MODERATOR STARTS THE GAME")
        with client.websocket_connect("/ws") as admin_ws:
            admin_ws.send_json({
                JSONFields.TYPE: Login.LOGIN,
                JSONFields.USERNAME: "dryrunadmin",
                JSONFields.PASSWORD: "dryrun-password",
            })
            admin_frame = admin_ws.receive_json()
            token = admin_frame.get(JSONFields.ADMIN_TOKEN)
            check("admin login issues a session token",
                  admin_frame.get(JSONFields.AUTHORISED) == Login.ACCEPTED and bool(token),
                  f"authorised={admin_frame.get(JSONFields.AUTHORISED)}")

            check("an unauthenticated start is refused",
                  client.post("/admin/rungame").status_code == 401)
            check("a forged token is refused",
                  client.post("/admin/rungame",
                              headers={"X-Admin-Token": "not-a-real-token"}
                              ).status_code == 403)

            dash = client.get("/admin/dashboard", headers={"X-Admin-Token": token})
            check("admin dashboard is reachable", dash.status_code == 200)
            if dash.status_code == 200:
                body = dash.json()
                check("dashboard reports every team",
                      body.get("total_teams") == args.teams,
                      f"total_teams={body.get('total_teams')}")
                check("dashboard reports image provider health",
                      isinstance(body.get("image_providers"), list))

            started = client.post("/admin/rungame", headers={"X-Admin-Token": token})
            check("moderator can start the game",
                  started.status_code == 200, started.text[:120])

            section("4. ROUNDS PLAY THROUGH")
            round_deadline = time.time() + args.game_timeout
            last_state = None
            while time.time() < round_deadline:
                if server.game_state == GameState.GAME_OVER:
                    break
                if server.current_round != last_state:
                    last_state = server.current_round
                    print(f"  round {server.current_round}/{server.total_rounds} ...")
                time.sleep(0.5)

            check("the game reached GAME_OVER",
                  server.game_state == GameState.GAME_OVER,
                  f"state={server.game_state}, round={server.current_round}, "
                  f"budget was {args.game_timeout}s")
            check("every round was played",
                  all(len(t.score) == args.rounds
                      for t in server.teams if t.id in server.participating_team_ids),
                  f"score list lengths: "
                  f"{sorted({len(t.score) for t in server.teams})}")
            print(f"  {stats['generated']} image generations, "
                  f"{stats['scored']} scoring calls")

        stop_event.set()
        for t in threads:
            t.join(timeout=5)

    if args.real_images:
        report_real_run(args, server, stats)

    section("5. LEADERBOARD AND QUALIFICATION")
    leaderboard = server.leaderboard()
    check("leaderboard covers every participating team",
          len(leaderboard) == len(server.participating_team_ids),
          f"{len(leaderboard)} entries, {len(server.participating_team_ids)} participated")
    check("leaderboard is sorted by score, descending",
          all(leaderboard[i]["score"] >= leaderboard[i + 1]["score"]
              for i in range(len(leaderboard) - 1)))
    check("ranks are 1-based and tie-aware",
          leaderboard and leaderboard[0]["rank"] == 1 and
          all(e["rank"] <= len(leaderboard) for e in leaderboard))
    check("no team exceeds the documented maximum of 100 per round",
          all(s <= 100 for t in server.teams for s in t.score))

    section("6. PHASE 2 HANDOFF FILES")
    csv_path = game.PHASE2_ROSTER_CSV
    scores_path = game.PHASE2_SCORES_FILE
    check("roster_cache_phase2.csv was written", os.path.exists(csv_path), csv_path)
    check("phase1_scores.json was written", os.path.exists(scores_path), scores_path)

    rows = []
    if os.path.exists(csv_path):
        with open(csv_path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            check("CSV header is exactly what CTFd's user import expects",
                  reader.fieldnames == ["name", "email", "password"],
                  f"got {reader.fieldnames}")
            rows = list(reader)

        expected_qualified = -(-len(leaderboard) // 2)   # ceil, top 50%
        check("about half the field qualified",
              len(rows) >= expected_qualified,
              f"{len(rows)} rows, ceil(half) = {expected_qualified} "
              f"(ties at the cutoff also advance)")
        check("every CSV row is complete",
              all(r["name"] and r["email"] and r["password"] for r in rows))
        # CTFd rejects an import outright on a duplicate, and it does so
        # partway through -- leaving some teams imported and some not, which
        # is far worse to unpick at the event than a clean failure.
        check("no duplicate emails in the CSV",
              len({r["email"] for r in rows}) == len(rows))
        check("no duplicate names in the CSV",
              len({r["name"] for r in rows}) == len(rows))
        check("passwords are unique per team",
              len({r["password"] for r in rows}) == len(rows))
        check("emails match the team<id>@example.invalid pattern "
              "final_leaderboard.py joins on",
              all(r["email"].startswith("team") and
                  r["email"].endswith("@example.invalid") for r in rows))

    if os.path.exists(scores_path):
        with open(scores_path, encoding="utf-8") as fh:
            carried = json.load(fh)
        check("phase1_scores.json covers exactly the qualified teams",
              len(carried) == len(rows),
              f"{len(carried)} scores vs {len(rows)} CSV rows")
        check("carried scores are cumulative, not single-round",
              all(float(v) <= 100 * args.rounds for v in carried.values()) and
              (max((float(v) for v in carried.values()), default=0) > 100
               if args.rounds > 1 else True),
              f"max carried score {max((float(v) for v in carried.values()), default=0):.1f}, "
              f"single-round max would be 100")

    section("7. CHECKPOINT")
    state_file = os.path.join(env["DWR_STATE_DIR"], "game_state.json")
    check("game_state.json checkpoint was written", os.path.exists(state_file))
    if os.path.exists(state_file):
        with open(state_file, encoding="utf-8") as fh:
            checkpoint = json.load(fh)
        check("checkpoint recorded every round",
              len(checkpoint.get("rounds", {})) == args.rounds,
              f"{len(checkpoint.get('rounds', {}))} rounds recorded")
        check("checkpoint's last_completed_round is the final round",
              checkpoint.get("last_completed_round") == args.rounds - 1,
              f"last_completed_round={checkpoint.get('last_completed_round')}")

    section("8. final_leaderboard.py FINDS THE SCORES FILE")
    # The step-8 failure this dry run exists to catch. Run the real script with
    # no --phase1-scores argument, from the working directory the runbook
    # specifies, and confirm it does not die looking in the wrong place.
    verify_leaderboard_default_path(scores_path)

    section("SUMMARY")
    print(f"  {len(PASSED)} passed, {len(FAILED)} failed")
    for line in FAILED:
        print(f"    FAILED: {line}")
    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print(f"\n  artifacts kept in {workdir}")
    return 1 if FAILED else 0


def report_real_run(args, server, stats):
    """What a billed run buys you that a stubbed one cannot.

    The stub produces scores by hashing, so it proves the plumbing carries a
    number from end to end and nothing more. Real images scored by real CLIP
    answer a question no other test in this repo touches: *is the scoring
    usable as a ranking?*

    That matters because the qualification cut is a median split. If real
    play compresses every team into a five-point band, the top-50% boundary
    is being decided by noise rather than by skill, and half the field is
    eliminated on a coin flip. It is much better to discover that here than
    from the standings.
    """
    import statistics

    section("4b. WHAT THE REAL PROVIDER AND REAL SCORING PRODUCED")

    print(f"  images generated   {stats['generated']}")
    if stats["budget_refusals"]:
        print(f"  refused (at cap)   {stats['budget_refusals']}")
    if args.cost_per_image:
        print(f"  spent              ~${stats['generated'] * args.cost_per_image:.2f}")

    # Look at the generated files that actually landed on disk. A provider
    # that quietly starts returning near-identical images under load passes
    # every latency and status check while making the game unscoreable.
    from paths import GENERATED_IMAGE_DIR
    try:
        files = [f for f in os.listdir(GENERATED_IMAGE_DIR) if not f.endswith(".tmp")]
        sizes = [os.path.getsize(os.path.join(GENERATED_IMAGE_DIR, f)) for f in files]
    except OSError:
        files, sizes = [], []

    if sizes:
        print(f"  files on disk      {len(files)}")
        print(f"  size               min {min(sizes)/1024:.0f} KB, "
              f"median {statistics.median(sizes)/1024:.0f} KB, "
              f"max {max(sizes)/1024:.0f} KB")
        print(f"  total written      {sum(sizes)/1024/1024:.1f} MB "
              f"({sum(sizes)/len(sizes)*3500/1024/1024/1024:.2f} GB projected "
              f"for a full 3500-image event)")
        check("generated images are not suspiciously uniform in size",
              len(set(s // 4096 for s in sizes)) > 1 or len(sizes) < 5,
              "near-identical file sizes can mean the provider is returning a "
              "placeholder")

    round_scores = [s for team in server.teams for s in team.score]
    if round_scores:
        spread = max(round_scores) - min(round_scores)
        print(f"\n  round scores       n={len(round_scores)}  "
              f"min {min(round_scores):.1f}  "
              f"median {statistics.median(round_scores):.1f}  "
              f"max {max(round_scores):.1f}")
        print(f"  spread             {spread:.1f} points, "
              f"stdev {statistics.pstdev(round_scores):.1f}")

        # The number that decides whether the qualification cut is meaningful.
        totals = sorted((server.team_total(t) for t in server.teams
                         if t.id in server.participating_team_ids), reverse=True)
        if len(totals) >= 4:
            cut = -(-len(totals) // 2)
            margin = totals[cut - 1] - totals[cut] if cut < len(totals) else 0.0
            print(f"  qualification cut  {totals[cut-1]:.1f} vs "
                  f"{totals[cut]:.1f} -- margin {margin:.2f} points")
            check("the top-50% cut is decided by more than rounding",
                  margin >= 0.5,
                  f"only {margin:.2f} points separate the last qualifier from "
                  f"the first eliminated team; with real play this close, the "
                  f"cut is noise")

        check("real CLIP scores spread across a usable range",
              spread >= 15,
              f"only {spread:.1f} points between the best and worst round. A "
              f"narrow band makes the median split arbitrary -- consider "
              f"whether the reference images are too similar to each other")
        check("no round scored a suspicious perfect 100",
              not any(s >= 99.99 for s in round_scores),
              "a 100 usually means the final image is the reference image, "
              "i.e. no generation succeeded that round")


def verify_leaderboard_default_path(dryrun_scores_path):
    """Does final_leaderboard.py's default --phase1-scores resolve to a real
    file when run the way the runbook says to run it?

    Checked by importing the script and reading the constant rather than by
    executing it, because executing it would need a live CTFd. The path
    resolution is the part that has been wrong; the CTFd half is covered by
    tools/ctfd_preflight.py.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    script = os.path.join(repo_root, "DWR_Phase_2", "Dark_Web_Rises",
                          "scripts", "final_leaderboard.py")
    if not os.path.exists(script):
        print(f"  [skip] Phase 2 checkout not found at {script}")
        print("         (expected as a sibling of Dark_Web_Rises_Phase1)")
        return

    documented_cwd = os.path.dirname(os.path.dirname(script))
    probe = subprocess.run(
        [sys.executable, "-c",
         "import runpy,sys,json,os;"
         "sys.argv=['final_leaderboard.py'];"
         "m=runpy.run_path(sys.argv[0] if False else r'''%s''', run_name='__probe__');"
         "print(json.dumps({'default': m['DEFAULT_PHASE1_SCORES'],"
         "'exists': os.path.exists(m['DEFAULT_PHASE1_SCORES'])}))" % script],
        cwd=documented_cwd, capture_output=True, text=True, timeout=60,
    )
    if probe.returncode != 0:
        check("final_leaderboard.py is importable", False, probe.stderr.strip()[:200])
        return
    info = json.loads(probe.stdout.strip().splitlines()[-1])
    print(f"  runbook CWD: {documented_cwd}")
    print(f"  resolves to: {info['default']}")
    check("final_leaderboard.py's default phase1_scores path resolves inside "
          "the project",
          "Dark_Web_Rises" in info["default"] and
          info["default"].count("Dark_Web_Rises") >= 1 and
          os.path.isdir(os.path.dirname(os.path.dirname(info["default"]))),
          info["default"])
    check("...and that file exists after a real game over",
          info["exists"],
          "run Phase 1 to game over first, or the path is still wrong")


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--teams", type=int, default=175, help="teams (default: the event's 175)")
    p.add_argument("--members", type=int, default=4)
    p.add_argument("--rounds", type=int, default=5)
    p.add_argument("--turn-seconds", type=float, default=3.0,
                   help="compressed per-turn budget (default 3; production is 90)")
    p.add_argument("--realtime", action="store_true",
                   help="use production timings -- takes as long as the real event")
    p.add_argument("--image-latency", type=float, default=0.05,
                   help="simulated seconds per image generation")
    p.add_argument("--image-failure-rate", type=float, default=0.02,
                   help="fraction of generations that fail, exercising the "
                        "'keep the previous image' path in models/team.py")
    p.add_argument("--prompt-delay", type=float, default=0.0,
                   help="seconds a simulated player waits before prompting")
    p.add_argument("--real-scoring", action="store_true",
                   help="use the real CLIP model instead of the score stub (slow)")
    p.add_argument("--real-images", action="store_true",
                   help="GENERATE REAL IMAGES through the configured provider "
                        "chain. THIS COSTS MONEY. Bounded by --max-images.")
    p.add_argument("--max-images", type=int, default=500,
                   help="hard cap on billed generations when --real-images is "
                        "set (default 500). Generations past the cap return "
                        "None, which the game already handles, so the run "
                        "degrades rather than crashing.")
    p.add_argument("--cost-per-image", type=float, default=0.0,
                   help="USD per image, for the spend report at the end")
    p.add_argument("--login-timeout", type=float, default=180.0)
    p.add_argument("--game-timeout", type=float, default=900.0)
    p.add_argument("--seed", type=int, default=20260818)
    p.add_argument("--log-level", default="WARNING")
    p.add_argument("--workdir", help="use this directory instead of a temp one")
    p.add_argument("--keep", action="store_true", help="keep the artifacts")
    args = p.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
