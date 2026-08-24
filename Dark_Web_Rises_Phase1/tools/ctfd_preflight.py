"""Check the CTFd half of the event before you need it, not during.

    python tools/ctfd_preflight.py                       # before the event
    python tools/ctfd_preflight.py --after-import        # after step 6
    python tools/ctfd_preflight.py --after-import --csv ../state/roster_cache_phase2.csv

Runbook steps 6 and 8 are the two that cannot be rehearsed by
tools/e2e_dryrun.py, because they need a live CTFd. They are also the two with
the least slack: step 6 happens with the qualified teams standing there, and
step 8 happens with everyone waiting for the final scoreboard. Everything this
script checks is something that has a fix, but only if you know about it more
than five minutes beforehand.

The failures it is looking for
------------------------------
    token          an access token created BEFORE an import is destroyed by
                   that import -- CTFd replaces the token table. You get a 403
                   at step 8 and assume the token is wrong rather than gone.
    user mode      the CSV is a *user* import. In teams mode the accounts land
                   somewhere final_leaderboard.py does not look, and it
                   produces a leaderboard of zeros with no error at all.
    end time       without one, ctf_ended() is never true, the scoreboard
                   never flips to the concluded panel, and the leaderboard
                   button never appears.
    submission     the per-account rate limit is shared by four teammates on
    rate limit     one login. At the default 10/min a team gets 2.5 guesses
                   each per minute.
    challenge      final_leaderboard.py normalises against the summed
    points         challenge total. If that is 0, or if challenges are hidden
                   after being solved, the normalisation is wrong.
    scoreboard     if /api/v1/scoreboard is capped or paginated on this
    coverage       version, teams past the cap silently score 0 in phase 2.
    import         did every row in the CSV actually become an account?
    coverage       a partial import shows up as "no CTFd account" warnings at
                   step 8, by which time the CSV may already have been moved.

Read-only: this makes GET requests and changes nothing.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

EMAIL_RE = re.compile(r"^team(\d+)@example\.invalid$", re.IGNORECASE)

PASSED, WARNED, FAILED = [], [], []


def ok(label, detail=""):
    PASSED.append(label)
    print(f"  [ OK ] {label}" + (f"  -- {detail}" if detail else ""))


def warn(label, detail=""):
    WARNED.append(label)
    print(f"  [WARN] {label}" + (f"\n         {detail}" if detail else ""))


def fail(label, detail=""):
    FAILED.append(label)
    print(f"  [FAIL] {label}" + (f"\n         {detail}" if detail else ""))


def api(base_url, path, token, allow_error=False):
    url = f"{base_url.rstrip('/')}/api/v1/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if allow_error:
            return None, (exc.code, detail)
        raise SystemExit(
            f"CTFd returned HTTP {exc.code} for {url}\n{detail}\n\n"
            + ("An access token created before a CSV import is destroyed by that\n"
               "import -- CTFd replaces the token table. Create a fresh one at\n"
               "Settings -> Access Tokens and re-export CTFD_ADMIN_TOKEN.\n"
               if exc.code in (401, 403) else "")
        )
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"Could not reach CTFd at {url}: {exc.reason}\n"
            "Is the stack up?  docker compose ps"
        )


def paginated(base_url, path, token):
    page = 1
    while True:
        sep = "&" if "?" in path else "?"
        payload, _ = api(base_url, f"{path}{sep}page={page}", token)
        rows = payload.get("data", [])
        yield from rows
        meta = (payload.get("meta") or {}).get("pagination") or {}
        nxt = meta.get("next")
        if not nxt or not rows:
            return
        page = nxt


def config_value(base_url, token, key):
    payload, err = api(base_url, f"configs/{key}", token, allow_error=True)
    if err:
        return None
    return (payload.get("data") or {}).get("value")


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ctfd-url", default=os.getenv("CTFD_URL", "http://localhost:8080"))
    p.add_argument("--token", default=os.getenv("CTFD_ADMIN_TOKEN"))
    p.add_argument("--after-import", action="store_true",
                   help="also check that the qualified teams actually imported")
    p.add_argument("--csv", help="roster_cache_phase2.csv, to compare against CTFd")
    p.add_argument("--expect-teams", type=int, default=0,
                   help="how many teams you expect to have qualified")
    p.add_argument("--concurrent-per-account", type=int, default=4,
                   help="teammates sharing one login (default 4)")
    args = p.parse_args()

    if not args.token:
        raise SystemExit(
            "No CTFd admin token. Settings -> Access Tokens, then:\n"
            '  $env:CTFD_ADMIN_TOKEN = "ctfd_xxxxx"   (PowerShell -- `set` does NOT work)\n'
            "  export CTFD_ADMIN_TOKEN=ctfd_xxxxx     (bash)"
        )

    print(f"CTFd: {args.ctfd_url}\n")

    print("-- access -----------------------------------------------------------")
    payload, err = api(args.ctfd_url, "users?per_page=1", args.token, allow_error=True)
    if err:
        fail(f"admin token rejected (HTTP {err[0]})",
             "A token created before a CSV import is invalidated by that import. "
             "Create a fresh one at Settings -> Access Tokens.")
        return 1
    ok("admin token accepted")

    print("\n-- configuration ----------------------------------------------------")
    mode = str(config_value(args.ctfd_url, args.token, "user_mode") or "").lower()
    if mode == "users":
        ok("user mode is 'users'", "matches the name,email,password CSV import")
    elif mode == "teams":
        fail("user mode is 'teams'",
             "final_score.py writes a USER import (name,email,password), and\n"
             "         final_leaderboard.py looks the accounts up under /api/v1/users.\n"
             "         In teams mode the import lands elsewhere and the final\n"
             "         leaderboard comes out all zeros with no error. Switch to Users\n"
             "         BEFORE importing -- switching afterwards deletes accounts.")
    else:
        warn(f"could not read user mode (got {mode!r})",
             "check Admin -> Config -> User Mode by hand")

    start = config_value(args.ctfd_url, args.token, "start")
    end = config_value(args.ctfd_url, args.token, "end")
    if end:
        ok("an end time is set", f"end={end}")
    else:
        fail("no end time set",
             "ctf_ended() is never true without one, so the scoreboard never flips\n"
             "         to the concluded panel and the final-leaderboard button never\n"
             "         appears. Admin -> Config -> Date & Time.")
    if not start:
        warn("no start time set", "usually fine, but confirm it is deliberate")

    limit = config_value(args.ctfd_url, args.token, "incorrect_submissions_per_min")
    try:
        limit_n = int(limit)
    except (TypeError, ValueError):
        limit_n = None
    if limit_n is None:
        warn("could not read incorrect_submissions_per_min")
    elif limit_n < 10 * args.concurrent_per_account:
        per_person = limit_n / max(1, args.concurrent_per_account)
        warn(f"incorrect_submissions_per_min is {limit_n}",
             f"That limit is per ACCOUNT, and {args.concurrent_per_account} teammates\n"
             f"         share one login -- so each player gets about {per_person:.1f} wrong\n"
             f"         guesses a minute before the whole team is throttled. Raise it to\n"
             f"         ~{10 * args.concurrent_per_account} (Admin -> Config -> Settings).")
    else:
        ok(f"incorrect_submissions_per_min is {limit_n}",
           f"~{limit_n / args.concurrent_per_account:.0f}/min per player at "
           f"{args.concurrent_per_account} per team")

    print("\n-- challenges -------------------------------------------------------")
    # ?view=admin is essential: without it CTFd returns only currently-visible
    # challenges, so anything hidden after being solved drops out of the total,
    # the denominator shrinks, and normalised scores climb past 1.0.
    challenges = list(paginated(args.ctfd_url, "challenges?view=admin", args.token))
    total_points = sum(int(c.get("value") or 0) for c in challenges)
    if not challenges:
        fail("no challenges found", "has the export zip been imported?")
    elif total_points == 0:
        fail("challenges total 0 points",
             "final_leaderboard.py normalises phase 2 against this; 0 makes the\n"
             "         maths undefined and it will refuse to run.")
    else:
        ok(f"{len(challenges)} challenges, {total_points} points total",
           "this is the phase 2 denominator")
    hidden = [c for c in challenges if str(c.get("state")) == "hidden"]
    if hidden:
        warn(f"{len(hidden)} challenge(s) are hidden",
             "They still count toward the total above (?view=admin), which is\n"
             "         correct -- but confirm they are meant to be unreachable.")

    print("\n-- accounts ---------------------------------------------------------")
    users = list(paginated(args.ctfd_url, "users?view=admin", args.token))
    imported = [u for u in users if EMAIL_RE.match((u.get("email") or "").strip())] \
        if any(u.get("email") for u in users) else []
    # /api/v1/users hardcodes the non-admin schema, so emails are usually
    # absent even for an admin token. Fall back to counting by name shape, and
    # say which method was used rather than quietly reporting a wrong number.
    if imported:
        ok(f"{len(imported)} account(s) match team<id>@example.invalid",
           "matched on email")
        detected = len(imported)
    else:
        admins = [u for u in users if u.get("type") == "admin"]
        detected = len(users) - len(admins)
        print(f"         (emails are not exposed by /api/v1/users on this CTFd "
              f"version;\n          counting non-admin accounts instead)")
        ok(f"{detected} non-admin account(s), {len(admins)} admin(s)")

    if args.after_import:
        expected = args.expect_teams
        csv_rows = []
        if args.csv:
            if not os.path.exists(args.csv):
                fail(f"CSV not found at {args.csv}")
            else:
                with open(args.csv, newline="", encoding="utf-8") as fh:
                    csv_rows = list(csv.DictReader(fh))
                expected = expected or len(csv_rows)
                ok(f"CSV has {len(csv_rows)} qualified team(s)", args.csv)
        if expected:
            if detected == expected:
                ok(f"import is complete: {detected}/{expected} accounts")
            elif detected < expected:
                fail(f"import is INCOMPLETE: {detected}/{expected} accounts",
                     "Every missing team scores 0 for phase 2 and shows up at step 8\n"
                     "         as a 'no CTFd account' warning. Re-import before the CTF\n"
                     "         starts -- afterwards you would also be discarding solves.")
            else:
                warn(f"{detected} accounts but only {expected} expected",
                     "leftover test accounts? Runbook step 1 clears them.")

        # Does the scoreboard actually return every account? If this CTFd caps
        # or paginates the scoreboard, teams past the cap score 0 in the final
        # leaderboard with nothing anywhere to say so.
        board, _ = api(args.ctfd_url, "scoreboard", args.token)
        rows = board.get("data", [])
        solved_any = [u for u in rows if int(u.get("score") or 0) > 0]
        if rows and detected and len(rows) < min(detected, len(solved_any) or detected):
            warn(f"scoreboard returned {len(rows)} rows for {detected} accounts",
                 "The scoreboard omits accounts with no solves, which is normal\n"
                 "         before the CTF starts. If this persists once teams are solving,\n"
                 "         the endpoint is capped and final_leaderboard.py would miss\n"
                 "         teams past the cap.")
        else:
            ok(f"scoreboard returned {len(rows)} row(s)",
               f"{len(solved_any)} with a non-zero score")

    print("\n" + "=" * 72)
    print(f"  {len(PASSED)} ok, {len(WARNED)} warning(s), {len(FAILED)} failure(s)")
    if FAILED:
        print("\n  Fix the failures before the event. Each one produces a wrong or")
        print("  missing final scoreboard rather than an error you would notice.")
    print("=" * 72)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
