"""The whole team sees the round's final image -- proven over real websockets.

    python tools/test_round_image.py

No network, no provider, no CLIP, no cost. Image generation and scoring are
stubbed, so this asserts the *plumbing*: that the image the chain ended on
reaches every connected member in the ROUND_OVER frame.

Why this needs a test at all
---------------------------
During a turn, `IMAGE` carries the real image only for the player whose turn
it is; everyone else gets WAIT_YOUR_TURN_IMAGE. So for three of the four
members of a team, the ROUND_OVER frame is the ONLY time they ever see what
their team actually produced. If that field is missing, empty, or carries the
placeholder, the feature is silently absent for exactly the people it was
added for -- and it looks fine when the developer tests it alone, because a
one-player team is always the active player.

What this pins down
-------------------
  1. every connected member receives round_image, not just the last player
  2. it is the image the chain ENDED on, not the reference it started from
  3. it is not the "wait your turn" placeholder
  4. a second round reports that round's image, not the previous one
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSED, FAILED = [], []

REFERENCE_IMAGE = "/static/images/REFERENCE.png"
GENERATED = {}          # prompt -> image path handed back by the fake provider


def check(label, condition, detail=""):
    (PASSED if condition else FAILED).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    return bool(condition)


def main():
    workdir = tempfile.mkdtemp(prefix="dwr-roundimg-")
    state_dir = os.path.join(workdir, "state")
    os.makedirs(state_dir, exist_ok=True)

    # One team, two members. Two so the assertion "everyone got it" is
    # actually testable -- with one member it is vacuously true, which is
    # exactly the blind spot this feature has.
    roster = os.path.join(workdir, "roster.json")
    with open(roster, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "fetched_at": time.time(), "team_count": 1,
                   "players": [{"id": i, "username": f"p{i}", "password": f"pw{i}",
                                "team_id": 0} for i in range(2)]}, fh)

    os.environ.update({
        "DWR_STATE_DIR": state_dir,
        "ROSTER_SOURCE": "cache",
        "ROSTER_CACHE_FILE": roster,
        "MAX_MEMBERS_PER_TEAM": "2",
        "DWR_ADMINS": "a:b",
        "CORS_ALLOWED_ORIGINS": "http://localhost:5173",
        "IMAGE_PROVIDERS": "pollinations",
        "LOG_LEVEL": "CRITICAL",
        "TOTAL_ROUNDS": "2",
        "TIME_PER_ROUND": "6",
        "COUNTDOWN_SECONDS": "0",
        "ROUND_BREAK_SECONDS": "1",
    })

    import game
    import models.team as team_module
    import services.ai_handling as ai

    ai.warm_up_model = lambda: None
    game.warm_up_model = lambda: None

    # --- stub the provider and the scorer -----------------------------
    # Patched on models.team, not on services.ai_handling: team.py imports
    # these by name at module load, so rebinding the source module would not
    # change what run_round actually calls.
    async def fake_get_image(prompt):
        if prompt == "default_prompt":
            return REFERENCE_IMAGE
        path = f"/static/generated/{prompt.replace(' ', '_')}.png"
        GENERATED[prompt] = path
        return path

    async def fake_compare_image(penalty, original, current):
        return 42.0

    team_module.get_image = fake_get_image
    team_module.compare_image = fake_compare_image
    team_module.classify_prompt = lambda prompt: True

    from enumerations import (GamePlay, GameState, JSONFields, Login,
                              Responses, TeamState)
    from starlette.testclient import TestClient

    print("=" * 70)
    print("ROUND IMAGE -- the whole team sees where the chain ended")
    print("=" * 70)

    round_frames = {0: [], 1: []}   # uid -> ROUND_OVER frames received
    prompts_sent = {}               # uid -> prompt text

    def play(ws, uid, stop):
        """Behave like a player: answer probes, prompt when it is your turn."""
        while not stop.is_set():
            try:
                frame = ws.receive_json()
            except Exception:
                return
            ftype = frame.get(JSONFields.TYPE)

            if ftype == Responses.SESSION_PROBE:
                try:
                    ws.send_json({JSONFields.TYPE: Login.SESSION_PROBE_ACK})
                except Exception:
                    return
                continue

            if (ftype == Responses.GAME_STATE_RESPONSE
                    and frame.get(JSONFields.GAME_STATE) == GameState.ROUND_OVER):
                round_frames[uid].append(frame)
                continue

            # Our turn: send a prompt unique to this player and round.
            if frame.get(JSONFields.IS_PLAYER_TURN):
                text = f"player {uid} turn {len(round_frames[uid])}"
                prompts_sent[uid] = text
                try:
                    ws.send_json({JSONFields.TYPE: GamePlay.PROMPT_OUT,
                                  JSONFields.STATUS: GamePlay.PROMPTED,
                                  JSONFields.PROMPT: text})
                except Exception:
                    return

    with TestClient(game.app) as client:
        sockets, ctxs, threads = {}, {}, []
        stop = threading.Event()

        for uid in (0, 1):
            ctx = client.websocket_connect("/ws")
            ws = ctx.__enter__()
            ws.send_json({JSONFields.TYPE: Login.LOGIN,
                          JSONFields.USERNAME: f"p{uid}", JSONFields.PASSWORD: f"pw{uid}"})
            ws.receive_json()          # login response
            ctxs[uid], sockets[uid] = ctx, ws

        for uid in (0, 1):
            t = threading.Thread(target=play, args=(sockets[uid], uid, stop), daemon=True)
            t.start()
            threads.append(t)

        with client.websocket_connect("/ws") as admin:
            admin.send_json({JSONFields.TYPE: Login.LOGIN,
                             JSONFields.USERNAME: "a", JSONFields.PASSWORD: "b"})
            token = admin.receive_json().get(JSONFields.ADMIN_TOKEN)
            resp = client.post("/admin/rungame", headers={"X-Admin-Token": token})
            check("game started", resp.status_code == 200, f"HTTP {resp.status_code}")

            # 2 rounds x 2 players x 6s turns, plus breaks.
            deadline = time.time() + 75
            while time.time() < deadline:
                if len(round_frames[0]) >= 2 and len(round_frames[1]) >= 2:
                    break
                time.sleep(0.25)

        stop.set()
        for uid in (0, 1):
            try:
                ctxs[uid].__exit__(None, None, None)
            except Exception:
                pass

    print("\n-- 1. everyone got a round_over frame --------------------------------")
    check("player 0 received both rounds", len(round_frames[0]) >= 2,
          f"{len(round_frames[0])} frames")
    check("player 1 received both rounds", len(round_frames[1]) >= 2,
          f"{len(round_frames[1])} frames  -- not just the last player to prompt")

    if not (round_frames[0] and round_frames[1]):
        print("\n  no frames captured; the checks below cannot run")
        print("=" * 70)
        print(f"  {len(PASSED)} passed, {len(FAILED) + 4} failed")
        return 1

    print("\n-- 2. the field is present and populated -----------------------------")
    for uid in (0, 1):
        first = round_frames[uid][0]
        check(f"player {uid}: round_image present",
              JSONFields.ROUND_IMAGE in first)
        check(f"player {uid}: round_image is not empty",
              bool(first.get(JSONFields.ROUND_IMAGE)),
              str(first.get(JSONFields.ROUND_IMAGE))[:48])

    print("\n-- 3. it is the FINAL image, not the reference or the placeholder ----")
    img0 = round_frames[0][0].get(JSONFields.ROUND_IMAGE)
    check("not the reference image the round started from",
          img0 != REFERENCE_IMAGE,
          "otherwise the team is shown what it was copying, not what it made")
    check("not the wait-your-turn placeholder",
          img0 != team_module.WAIT_YOUR_TURN_IMAGE,
          "the placeholder is what non-active players see DURING the turn")
    check("it is an image the fake provider actually generated",
          img0 in GENERATED.values(), str(img0)[:48])

    print("\n-- 4. both members see the SAME image --------------------------------")
    check("player 0 and player 1 agree on round 1",
          round_frames[0][0].get(JSONFields.ROUND_IMAGE)
          == round_frames[1][0].get(JSONFields.ROUND_IMAGE),
          "it is one team, one chain, one final image")

    print("\n-- 5. round 2 reports round 2's image --------------------------------")
    r1 = round_frames[0][0].get(JSONFields.ROUND_IMAGE)
    r2 = round_frames[0][1].get(JSONFields.ROUND_IMAGE)
    check("the second round's image differs from the first",
          r1 != r2,
          "current_image is reused across rounds -- a stale value would show here")
    check("round numbers are 1 then 2",
          [f.get(JSONFields.ROUND) for f in round_frames[0][:2]] == [1, 2],
          str([f.get(JSONFields.ROUND) for f in round_frames[0][:2]]))

    print("\n" + "=" * 70)
    print(f"  {len(PASSED)} passed, {len(FAILED)} failed")
    for line in FAILED:
        print(f"    FAILED: {line}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
