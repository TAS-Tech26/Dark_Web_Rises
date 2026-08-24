"""A stale checkpoint must not silently end the event.

    python tools/test_checkpoint_guard.py

No network, no provider, no cost.

The failure being guarded against
---------------------------------
`state/game_state.json` is bind-mounted on purpose -- it holds the phase 2
handoff and must survive `docker compose down`. Which means it also survives
into the NEXT event, and the next rehearsal, and the run after the one you
abandoned halfway.

If that leftover file says the game is finished, pressing Start Game used to:

    play no rounds at all
    broadcast GAME_OVER to everyone connected
    write roster_cache_phase2.csv
    generate CTFd passwords
    tell whichever teams had a socket open that they had qualified

and none of it looks like an error. The dashboard shows a completed game. It
is only wrong if you happen to know that nobody played.

`_play_all_rounds` did notice -- it logged a warning and returned early. But
`start_games` calls `_finish_game()` from a `finally`, so returning early does
not abort the game, it completes it. The guard therefore has to sit at the
start endpoint, where a human is still in the loop.

What this pins down
-------------------
  1. no checkpoint            -> starts normally
  2. partial checkpoint       -> resumes (crash recovery must keep working)
  3. completed checkpoint     -> REFUSED, with an explanation
  4. TOTAL_ROUNDS changed     -> REFUSED (scores are not comparable)
  5. explicit override        -> allowed, for a genuine post-final-round crash
  6. corrupt checkpoint       -> starts fresh rather than blocking the event
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSED, FAILED = [], []


def check(label, condition, detail=""):
    (PASSED if condition else FAILED).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    return bool(condition)


def main():
    workdir = tempfile.mkdtemp(prefix="dwr-ckpt-")
    state_dir = os.path.join(workdir, "state")
    os.makedirs(state_dir, exist_ok=True)
    roster = os.path.join(workdir, "roster.json")
    with open(roster, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "fetched_at": time.time(), "team_count": 2,
                   "players": [{"id": i, "username": f"p{i}", "password": f"pw{i}",
                                "team_id": i // 2} for i in range(4)]}, fh)

    os.environ.update({
        "DWR_STATE_DIR": state_dir,
        "ROSTER_SOURCE": "cache",
        "ROSTER_CACHE_FILE": roster,
        "MAX_MEMBERS_PER_TEAM": "2",
        "DWR_ADMINS": "a:b",
        "CORS_ALLOWED_ORIGINS": "http://localhost:5173",
        "IMAGE_PROVIDERS": "pollinations",
        "LOG_LEVEL": "CRITICAL",
        "TOTAL_ROUNDS": "5",
    })

    import game
    import services.ai_handling as ai
    ai.warm_up_model = lambda: None
    game.warm_up_model = lambda: None

    state_file = game.STATE_FILE

    def write_checkpoint(last_completed, total_rounds=5, corrupt=False):
        if corrupt:
            with open(state_file, "w", encoding="utf-8") as fh:
                fh.write("{ this is not json")
            return
        payload = {
            "last_completed_round": last_completed,
            "total_rounds": total_rounds,
            "rounds": {str(r): {"0": 50.0} for r in range(last_completed + 1)},
            "inactive_teams": [],
        }
        with open(state_file, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def clear():
        if os.path.exists(state_file):
            os.remove(state_file)

    print("=" * 70)
    print("CHECKPOINT GUARD")
    print("=" * 70)

    print("\n-- 1. no checkpoint --------------------------------------------------")
    clear()
    ok, problem = game.checkpoint_status()
    check("a fresh install starts normally", ok, problem or "")

    print("\n-- 2. partial checkpoint (real crash recovery) -----------------------")
    write_checkpoint(last_completed=2, total_rounds=5)
    ok, problem = game.checkpoint_status()
    check("a half-finished game still resumes", ok,
          "rounds 0-2 scored, resuming at round 4 of 5")

    print("\n-- 3. completed checkpoint -------------------------------------------")
    write_checkpoint(last_completed=4, total_rounds=5)
    ok, problem = game.checkpoint_status()
    check("a finished game is REFUSED", not ok)
    check("the refusal says what would have happened",
          problem and "straight to game over" in problem,
          (problem or "").splitlines()[1][:66] if problem else "no message")
    check("the refusal says how to fix it",
          problem and "Delete" in problem and "DWR_RESUME_COMPLETED" in problem)

    print("\n-- 4. TOTAL_ROUNDS changed under the checkpoint ----------------------")
    # Exactly the situation a one-round manual test leaves behind.
    write_checkpoint(last_completed=0, total_rounds=1)
    ok, problem = game.checkpoint_status()
    check("a checkpoint from a differently-shaped game is REFUSED", not ok)
    check("the refusal names both round counts",
          problem and "1-round" in problem and "5" in problem,
          (problem or "").splitlines()[0][:66] if problem else "")

    print("\n-- 5. explicit override ----------------------------------------------")
    write_checkpoint(last_completed=4, total_rounds=5)
    game.ALLOW_RESUME_COMPLETED = True
    ok, problem = game.checkpoint_status()
    check("DWR_RESUME_COMPLETED lets a real post-final crash resume", ok,
          "the narrow case this is for")
    game.ALLOW_RESUME_COMPLETED = False

    print("\n-- 6. corrupt checkpoint ---------------------------------------------")
    write_checkpoint(0, corrupt=True)
    ok, problem = game.checkpoint_status()
    check("an unreadable checkpoint does not block the event", ok,
          "load_game_checkpoint ignores it and starts from round 0")

    print("\n-- 7. the endpoint actually refuses ----------------------------------")
    from starlette.testclient import TestClient
    from enumerations import JSONFields, Login, Responses
    write_checkpoint(last_completed=4, total_rounds=5)
    with TestClient(game.app) as client:
        with client.websocket_connect("/ws") as admin:
            admin.send_json({JSONFields.TYPE: Login.LOGIN,
                             JSONFields.USERNAME: "a", JSONFields.PASSWORD: "b"})
            frame = admin.receive_json()
            token = frame.get(JSONFields.ADMIN_TOKEN)
            check("admin logged in", bool(token))
            resp = client.post("/admin/rungame", headers={"X-Admin-Token": token})
            check("POST /admin/rungame is refused with 409",
                  resp.status_code == 409, f"got {resp.status_code}")
            check("the moderator is told why, not just 'error'",
                  "straight to game over" in resp.text,
                  resp.json().get("detail", "").splitlines()[0][:60]
                  if resp.status_code == 409 else resp.text[:60])
            check("the game did NOT enter game over",
                  game.server.game_state != 4,
                  f"game_state={game.server.game_state}")
            check("no phase 2 CSV was written",
                  not os.path.exists(game.PHASE2_ROSTER_CSV),
                  "credentials would have been issued to whoever was connected")

    print("\n" + "=" * 70)
    print(f"  {len(PASSED)} passed, {len(FAILED)} failed")
    for line in FAILED:
        print(f"    FAILED: {line}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
