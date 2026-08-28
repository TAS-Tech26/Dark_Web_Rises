"""Build, refresh and validate the roster cache -- outside the running app.

    python tools/roster_build.py check                      # is the cache right?
    python tools/roster_build.py from-csv                   # build from the CSV
    python tools/roster_build.py refresh                    # pull from Supabase
    python tools/roster_build.py synth --teams 175          # generate a test roster

Why this is a separate tool and not something the app does
-----------------------------------------------------------
The app already writes the cache after a successful Supabase fetch. In the
Docker deployment it cannot:

    docker-compose.yml:
      - ./roster_cache.json:/app/roster_cache.json:ro     <- read only
      - ROSTER_CACHE_FILE=/app/roster_cache.json

so `roster.write_cache()` fails with EROFS on every boot, logs an error, and
continues. The cache is therefore frozen at whatever was last written *on the
host*, and nothing running inside the container can refresh it.

That is a defensible choice -- a read-only mount of a credentials file is not
an accident -- but it has a consequence that has to be handled somewhere:
the fallback roster silently ages. If Supabase is unreachable on the morning
of the event, the app boots from that frozen file. If it is a 150-team cache
and 175 teams turn up, the app starts cleanly, reports "Server ready", and
100 attendees simply cannot log in. Nothing fails. Nothing warns. You find out
from the queue at the help desk.

`refresh` closes that gap by writing the cache from the host, where the file
is writable, so the fallback is current before the doors open.

`check` is the one to run on event morning. It compares what is actually in
the cache against what you expect to be there, and says so in numbers.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paths  # noqa: F401,E402  -- loads .env
import roster as roster_module  # noqa: E402


def _read(path):
    if not os.path.exists(path):
        raise SystemExit(f"No roster cache at {path}")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write(path, players, team_count, source_note):
    payload = {
        "version": roster_module.CACHE_VERSION,
        "fetched_at": time.time(),
        "team_count": team_count,
        "players": players,
    }
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    try:
        # Same permissions the app would have applied. This file is plaintext
        # attendee passwords; it should not be world readable just because it
        # was written by a different process than usual.
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass  # Windows and some mounts do not support it
    print(f"Wrote {len(players)} players across {team_count} teams to {path}")
    print(f"Source: {source_note}")


# ----------------------------------------------------------------------
def cmd_check(args):
    path = args.cache or roster_module.cache_path()
    payload = _read(path)
    players = payload.get("players", [])
    team_ids = [p["team_id"] for p in players]
    sizes = Counter(Counter(team_ids).values())
    distinct_teams = len(set(team_ids))
    declared = payload.get("team_count")
    age_hours = (time.time() - (payload.get("fetched_at") or 0)) / 3600

    print(f"cache file        {path}")
    print(f"cache version     {payload.get('version')}")
    print(f"fetched           {age_hours:.1f} hours ago")
    print(f"players           {len(players)}")
    print(f"declared teams    {declared}")
    print(f"distinct team ids {distinct_teams}")
    print(f"team sizes        {dict(sorted(sizes.items()))}  (size: count)")
    print()

    problems = []

    if declared != distinct_teams:
        problems.append(
            f"team_count says {declared} but only {distinct_teams} distinct team ids "
            f"appear. GameServer builds `declared` Team objects, so the extra ones "
            f"would be empty and would never be able to play."
        )

    # team_ids must be a contiguous 0..n-1 range: GameServer indexes
    # self.teams[team_id] directly and validates 0 <= team_id < max_teams, so a
    # gap means empty teams and an out-of-range id means a refusal to start.
    if team_ids:
        expected = set(range(declared or distinct_teams))
        actual = set(team_ids)
        if actual - expected:
            problems.append(
                f"team ids outside range(0, {declared}): {sorted(actual - expected)[:10]} "
                f"-- GameServer will refuse to start."
            )
        gaps = expected - actual
        if gaps:
            problems.append(
                f"{len(gaps)} team id(s) have no players: {sorted(gaps)[:10]}"
                f"{' ...' if len(gaps) > 10 else ''}"
            )

    usernames = [p["username"].strip().casefold() for p in players]
    duplicates = [name for name, n in Counter(usernames).items() if n > 1]
    if duplicates:
        problems.append(
            f"{len(duplicates)} duplicate username(s) -- GameServer raises on the "
            f"first one and the app will not boot: {duplicates[:5]}"
        )

    blank = [p["id"] for p in players if not p.get("password")]
    if blank:
        problems.append(f"{len(blank)} player(s) have an empty password: {blank[:5]}")

    cap = int(os.getenv("MAX_MEMBERS_PER_TEAM", "4"))
    oversized = [tid for tid, n in Counter(team_ids).items() if n > cap]
    if oversized:
        problems.append(
            f"{len(oversized)} team(s) exceed MAX_MEMBERS_PER_TEAM={cap} -- "
            f"GameServer raises at construction: {oversized[:5]}"
        )

    # The comparison that matters on the day.
    if args.expect_teams and distinct_teams != args.expect_teams:
        problems.append(
            f"EXPECTED {args.expect_teams} teams, cache has {distinct_teams}. "
            f"If Supabase is unreachable on the day the app boots from THIS file, "
            f"and {abs(args.expect_teams - distinct_teams) * cap} attendees would "
            f"have no account."
        )
    if args.expect_players and len(players) != args.expect_players:
        problems.append(
            f"EXPECTED {args.expect_players} players, cache has {len(players)}."
        )
    if age_hours > args.max_age_hours:
        problems.append(
            f"cache is {age_hours:.0f}h old (limit {args.max_age_hours:.0f}h). "
            f"Anyone who signed up since then is not in it. Run "
            f"`python tools/roster_build.py refresh`."
        )

    if problems:
        print("PROBLEMS")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("Roster cache looks correct and current.")
    return 0


def cmd_refresh(args):
    """Fetch from Supabase and write the cache from the host.

    Uses the app's own loader, so a roster this accepts is one the app will
    accept -- a separate implementation here could disagree with the running
    code in exactly the situation where that matters most.
    """
    os.environ["ROSTER_SOURCE"] = "supabase"
    if args.cache:
        os.environ["ROSTER_CACHE_FILE"] = args.cache

    try:
        bundle = roster_module.load_from_supabase(
            max_members_per_team=int(os.getenv("MAX_MEMBERS_PER_TEAM", "4"))
        )
    except Exception as exc:
        raise SystemExit(
            f"Could not load the roster from Supabase: {exc}\n\n"
            "Check SUPABASE_URL and SUPABASE_KEY in .env. Note that row-level "
            "security returns zero rows rather than an error, so an empty result "
            "usually means the key lacks read access rather than that the table "
            "is empty. `python tools/supabase_dryrun.py` diagnoses that."
        )

    for warning in bundle.warnings:
        print(f"warning: {warning}")

    path = args.cache or roster_module.cache_path()
    players = [
        {"id": pid, "username": entry[0], "password": entry[1], "team_id": entry[2]}
        for pid, entry in bundle.player_data.items()
    ]
    _write(path, players, bundle.team_count, "Supabase")
    print("\nNow verify it:")
    print(f"  python tools/roster_build.py check --expect-teams {bundle.team_count}")
    return 0


def cmd_from_csv(args):
    """Build the cache from the participant CSV. The production path.

    Uses the app's own loader, so a roster this accepts is one the app will
    accept. A separate implementation here could disagree with the running
    code in exactly the situation where that matters most.
    """
    os.environ["ROSTER_SOURCE"] = "csv"
    if args.csv:
        os.environ["ROSTER_CSV_FILE"] = args.csv
    if args.cache:
        os.environ["ROSTER_CACHE_FILE"] = args.cache
    if args.event:
        os.environ["DWR_EVENT_NAME"] = args.event
    if args.lenient:
        os.environ["ROSTER_CSV_STRICT"] = "false"

    path = os.environ.get("ROSTER_CSV_FILE", "participants.csv")
    try:
        bundle = roster_module.load_from_csv(
            max_members_per_team=int(os.getenv("MAX_MEMBERS_PER_TEAM", "4"))
        )
    except Exception as exc:
        raise SystemExit(f"Could not build the roster from {path}:\n\n{exc}")

    for warning in bundle.warnings:
        print(f"warning: {warning}")

    out = args.cache or roster_module.cache_path()
    players = [
        {"id": pid, "username": entry[0], "password": entry[1], "team_id": entry[2]}
        for pid, entry in bundle.player_data.items()
    ]
    _write(out, players, bundle.team_count, f"CSV {path}")
    print("\nNow verify it:")
    print(f"  python tools/roster_build.py check --expect-teams {bundle.team_count} "
          f"--expect-players {len(players)}")
    return 0


def cmd_synth(args):
    """A synthetic roster, for load tests and rehearsals.

    NOT for a real event -- the passwords are derived from the index and are
    therefore public. The filename default makes that hard to confuse with the
    real cache.
    """
    players = []
    pid = 0
    for team_id in range(args.teams):
        for seat in range(args.members):
            players.append({
                "id": pid,
                "username": f"{args.prefix}{pid:04d}",
                "password": f"pw{pid:04d}",
                "team_id": team_id,
            })
            pid += 1
    _write(args.out, players, args.teams,
           "SYNTHETIC -- rehearsal only, passwords are predictable")
    print("\nRun the app against it with:")
    print(f"  ROSTER_SOURCE=cache ROSTER_CACHE_FILE={args.out} "
          f"MAX_MEMBERS_PER_TEAM={args.members}")
    return 0


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="validate a roster cache against expectations")
    c.add_argument("--cache", help="path (default: ROSTER_CACHE_FILE or the state dir)")
    c.add_argument("--expect-teams", type=int,
                   default=int(os.getenv("EXPECTED_TEAMS", "0")) or None)
    c.add_argument("--expect-players", type=int,
                   default=int(os.getenv("EXPECTED_PLAYERS", "0")) or None)
    c.add_argument("--max-age-hours", type=float, default=48.0)
    c.set_defaults(func=cmd_check)

    r = sub.add_parser("refresh", help="pull the roster from Supabase and cache it")
    r.add_argument("--cache", help="where to write (default: ROSTER_CACHE_FILE)")
    r.set_defaults(func=cmd_refresh)

    f = sub.add_parser("from-csv",
                       help="build the roster from the participant CSV (production)")
    f.add_argument("--csv", help="participant CSV (default: ROSTER_CSV_FILE, "
                                 "else participants.csv)")
    f.add_argument("--cache", help="where to write (default: ROSTER_CACHE_FILE)")
    f.add_argument("--event", help="event name to filter on "
                                   "(default: DWR_EVENT_NAME, else 'Dark Web Rises')")
    f.add_argument("--lenient", action="store_true",
                   help="drop unusable rows instead of refusing. Those people "
                        "cannot log in -- only use this knowing that.")
    f.set_defaults(func=cmd_from_csv)

    s = sub.add_parser("synth", help="generate a synthetic roster for rehearsals")
    s.add_argument("--teams", type=int, default=175)
    s.add_argument("--members", type=int, default=4)
    s.add_argument("--prefix", default="player")
    s.add_argument("--out", default="roster_cache_synthetic.json")
    s.set_defaults(func=cmd_synth)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
