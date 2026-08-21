"""One account, one live session -- proven against the real websocket server.

    python tools/test_session_handling.py

No network, no provider, no cost. Spins up the actual FastAPI app with a small
roster and drives it over real websockets.

The policy
----------
ONE ACCOUNT, ONE DEVICE, and the FIRST device keeps it. A second login is
refused while the original session is still answering. To move devices, log
out on the first one.

What it pins down
-----------------
  1. a second login is REFUSED while the first device answers a probe
  2. the refusal is its own status, not "invalid credentials"
  3. logging out releases the account immediately
  4. a session that does NOT answer a probe is treated as gone, so a player
     whose phone died is not locked out of their own account
  5. the surviving session keeps working after another socket disconnects

(4) is the one that stops this policy becoming a help-desk queue. A registered
socket is not evidence of a live device -- TCP can take minutes to notice a
phone that walked out of wifi range -- so liveness is *asked about*, not
assumed.

(5) is a regression test. `_cleanup_connection` used to compare
`game.connected_sockets[uid]` against `team.connected_sockets[uid]`, which
`check_login` sets to the same object, so the "is a newer session active?"
guard was always False and never once fired. A closing socket therefore tore
down whichever session was live: still logged in on screen, no socket
registered on the server, no frames ever again. It looked like a frontend bug
and it was not.
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


def build_roster(path, teams=2, members=2):
    players = [
        {"id": pid, "username": f"player{pid:03d}", "password": f"pw{pid:03d}",
         "team_id": pid // members}
        for pid in range(teams * members)
    ]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "fetched_at": time.time(),
                   "team_count": teams, "players": players}, fh)


def main():
    workdir = tempfile.mkdtemp(prefix="dwr-session-")
    roster = os.path.join(workdir, "roster.json")
    build_roster(roster)

    os.environ.update({
        "DWR_STATE_DIR": os.path.join(workdir, "state"),
        "ROSTER_SOURCE": "cache",
        "ROSTER_CACHE_FILE": roster,
        "MAX_MEMBERS_PER_TEAM": "2",
        "DWR_ADMINS": "a:b",
        "CORS_ALLOWED_ORIGINS": "http://localhost:5173",
        "IMAGE_PROVIDERS": "pollinations",
        "LOG_LEVEL": "CRITICAL",
    })
    os.makedirs(os.environ["DWR_STATE_DIR"], exist_ok=True)

    import game
    from enumerations import JSONFields, Login, Responses
    from starlette.testclient import TestClient

    # Never touched -- no round is started -- but stubbed so importing the app
    # cannot reach for CLIP weights.
    import services.ai_handling as ai
    ai.warm_up_model = lambda: None
    game.warm_up_model = lambda: None

    server = game.server
    uid = 0
    username, password = "player000", "pw000"

    def login_frame():
        return {JSONFields.TYPE: Login.LOGIN,
                JSONFields.USERNAME: username, JSONFields.PASSWORD: password}

    def read_until(ws, wanted, limit=6):
        """Drain frames until one of `wanted` types arrives, or give up.

        Necessary because a login is followed by team-state broadcasts, so the
        frame being asserted on is rarely the next one off the wire.
        """
        for _ in range(limit):
            try:
                frame = ws.receive_json()
            except Exception:
                return None
            if frame.get(JSONFields.TYPE) in wanted:
                return frame
        return None

    def answer_probes(ws, frames=6):
        """Behave like the real client: reply to any probe, return other frames.

        The whole policy hinges on a session answering promptly, so a test
        socket that stays silent is not simulating a connected player -- it is
        simulating a dead one, and would "prove" the opposite of what it looks
        like it is proving.
        """
        seen = []
        for _ in range(frames):
            try:
                frame = ws.receive_json()
            except Exception:
                break
            if frame.get(JSONFields.TYPE) == Responses.SESSION_PROBE:
                ws.send_json({JSONFields.TYPE: Login.SESSION_PROBE_ACK})
            else:
                seen.append(frame)
        return seen

    import threading

    def keep_answering(ws, stop):
        """Answer probes continuously, the way a browser tab would."""
        while not stop.is_set():
            try:
                frame = ws.receive_json()
            except Exception:
                return
            if frame.get(JSONFields.TYPE) == Responses.SESSION_PROBE:
                try:
                    ws.send_json({JSONFields.TYPE: Login.SESSION_PROBE_ACK})
                except Exception:
                    return

    print("=" * 70)
    print("SESSION HANDLING -- one account, one device, first device holds it")
    print("=" * 70)

    with TestClient(game.app) as client:
        print("\n-- 1. the first device takes the account -----------------------------")
        first_ctx = client.websocket_connect("/ws")
        first = first_ctx.__enter__()
        first.send_json(login_frame())
        accepted = read_until(first, {Responses.LOGIN_RESPONSE})
        check("first device logs in",
              accepted and accepted.get(JSONFields.AUTHORISED) == Login.ACCEPTED)
        first_socket = server.connected_sockets.get(uid)
        check("server registered it", first_socket is not None)

        # From here the first device behaves like a real browser: it answers
        # probes. Without this thread it would look dead and the test would
        # measure the wrong branch entirely.
        stop = threading.Event()
        answerer = threading.Thread(target=keep_answering, args=(first, stop), daemon=True)
        answerer.start()

        print("\n-- 2. a second device is REFUSED while the first is live -------------")
        second_ctx = client.websocket_connect("/ws")
        second = second_ctx.__enter__()
        second.send_json(login_frame())
        refused = read_until(second, {Responses.LOGIN_RESPONSE}, limit=8)
        check("second device is refused",
              refused and refused.get(JSONFields.AUTHORISED) == Login.ALREADY_CONNECTED,
              f"authorised={refused.get(JSONFields.AUTHORISED) if refused else None!r}")
        check("the refusal is not 'invalid credentials'",
              refused and refused.get(JSONFields.AUTHORISED) != Login.DENIED,
              "a player told that will retype until the lockout fires")
        check("the refusal explains what to do",
              refused and "another device" in str(refused.get(JSONFields.MESSAGE, "")).lower(),
              str(refused.get(JSONFields.MESSAGE, ""))[:70] if refused else "")
        check("the first device still owns the session",
              server.connected_sockets.get(uid) is first_socket)
        second_ctx.__exit__(None, None, None)

        print("\n-- 3. logging out releases the account -------------------------------")
        stop.set()
        first.send_json({JSONFields.TYPE: Login.LOGOUT})
        for _ in range(60):
            if uid not in server.connected_players:
                break
            time.sleep(0.05)
        check("logout cleared the session", uid not in server.connected_players)
        first_ctx.__exit__(None, None, None)

        third_ctx = client.websocket_connect("/ws")
        third = third_ctx.__enter__()
        third.send_json(login_frame())
        after_logout = read_until(third, {Responses.LOGIN_RESPONSE}, limit=8)
        check("a different device can now log in",
              after_logout and after_logout.get(JSONFields.AUTHORISED) == Login.ACCEPTED,
              "this is the documented way to switch devices")
        third_socket = server.connected_sockets.get(uid)
        third_ctx.__exit__(None, None, None)

        print("\n-- 4. a DEAD session does not lock the player out --------------------")
        # A socket that never answers a probe stands in for a phone that left
        # wifi: registered on the server, nobody behind it.
        for _ in range(60):
            if uid not in server.connected_players:
                break
            time.sleep(0.05)
        dead_ctx = client.websocket_connect("/ws")
        dead = dead_ctx.__enter__()
        dead.send_json(login_frame())
        read_until(dead, {Responses.LOGIN_RESPONSE})
        check("the 'dead' device holds the session",
              server.connected_sockets.get(uid) is not None)

        # Nobody answers probes on `dead` from here on.
        rescue_ctx = client.websocket_connect("/ws")
        rescue = rescue_ctx.__enter__()
        started = time.perf_counter()
        rescue.send_json(login_frame())
        rescued = read_until(rescue, {Responses.LOGIN_RESPONSE}, limit=8)
        waited = time.perf_counter() - started
        check("the player gets in when the old session does not answer",
              rescued and rescued.get(JSONFields.AUTHORISED) == Login.ACCEPTED,
              f"took {waited:.1f}s -- the probe timeout, not a lockout")
        check("and it took about the probe timeout, not minutes",
              waited < 10.0, f"{waited:.1f}s")
        rescue_socket = server.connected_sockets.get(uid)

        print("\n-- 5. the survivor outlives the retired socket -----------------------")
        try:
            dead_ctx.__exit__(None, None, None)
        except Exception:
            pass
        for _ in range(50):
            if server.connected_sockets.get(uid) is not rescue_socket:
                break
            time.sleep(0.02)
        check("closing the retired socket did NOT tear down the live session",
              server.connected_sockets.get(uid) is rescue_socket,
              "regression: the cleanup guard never fired and evicted the survivor")
        check("the live session is still in its team's socket map",
              uid in server.teams[0].connected_sockets,
              "without this the player receives no further frames -- the ghost")
        try:
            rescue_ctx.__exit__(None, None, None)
        except Exception:
            pass

    print("\n" + "=" * 70)
    print(f"  {len(PASSED)} passed, {len(FAILED)} failed")
    for line in FAILED:
        print(f"    FAILED: {line}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
