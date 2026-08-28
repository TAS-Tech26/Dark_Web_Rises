"""Build the roster, refuse it if it needs a human, ship it to the VM.

    python deploy_roster.py --check                 # build and report, ship nothing
    python deploy_roster.py                         # build, ship, restart, verify
    python deploy_roster.py --force                 # ship despite warnings

Written for the two-hour window between registrations closing and the event
starting. Everything in that window that can be a decision made in advance
should be; everything that can be one command instead of seven should be.

The seven steps this replaces
-----------------------------
    run roster_csv.py
    read the warnings, decide what to do
    scp the json to the VM
    docker compose cp it into the container
    restart dwr
    read the logs to see if it took
    compare the numbers against what you expected

Six of those are mechanical. One -- deciding what to do about a warning --
is not, and this stops dead there rather than shipping past it.

WHAT STOPS IT
-------------
Anything that needs a person:

  oversized team      someone must split it or drop a member. Raising
                      MAX_MEMBERS_PER_TEAM instead is NOT a fix: the
                      short-handed penalty in Team._run_round is computed
                      against that number, so raising it to 5 penalises
                      every 4-person team for a member they never had.
  admin name clash    rename the admin in .env, or the player in the sheet.
  no phone            that person's password is firstname@00000. Someone has
                      to hand them a password or get their number.

Duplicate names do NOT stop it: they are resolved automatically by suffixing.
But you still have to TELL those players, so they are printed loudly and
counted in the summary.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DEFAULT_VM = "ubuntu@206.1.59.57"
DEFAULT_REMOTE_DIR = "~/Dark_Web_Rises/Dark_Web_Rises_Phase1"


def run(cmd, capture=True):
    """Run a command, returning (rc, stdout+stderr)."""
    proc = subprocess.run(
        cmd, shell=isinstance(cmd, str),
        capture_output=capture, text=True,
    )
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xlsx", default="active_registrations_2026-08-27.xlsx")
    ap.add_argument("--out", default="roster_cache.json")
    ap.add_argument("--event", default=os.getenv("DWR_EVENT_NAME", "Dark Web Rises"))
    ap.add_argument("--max-members", type=int,
                    default=int(os.getenv("MAX_MEMBERS_PER_TEAM", "4")))
    ap.add_argument("--vm", default=DEFAULT_VM)
    ap.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    ap.add_argument("--check", action="store_true",
                    help="build and report only. Nothing leaves this machine.")
    ap.add_argument("--force", action="store_true",
                    help="ship even with warnings that need a person. Use only "
                         "when you have decided to run the event without them.")
    args = ap.parse_args()

    started = time.time()
    import roster_csv

    print("=" * 68)
    print("  1/5  BUILD")
    print("=" * 68)
    if not os.path.exists(args.xlsx):
        raise SystemExit(f"No such file: {args.xlsx}")

    r = roster_csv.build(args.xlsx, args.event, args.max_members)
    payload = r["payload"]
    n_players = len(payload["players"])
    n_teams = payload["team_count"]

    sizes = {}
    for s in r["team_sizes"]:
        sizes[s] = sizes.get(s, 0) + 1

    print(f"  teams   : {n_teams}")
    print(f"  players : {n_players}")
    print(f"  sizes   : {sizes}")

    # --- the decision point -------------------------------------------
    blockers = []
    if r["oversized"]:
        blockers.append(f"{len(r['oversized'])} team(s) over {args.max_members} members")
    if r["admin_clashes"]:
        blockers.append(f"{len(r['admin_clashes'])} member(s) named like an admin")
    if r["no_phone"]:
        blockers.append(f"{len(r['no_phone'])} member(s) with no phone number")

    if r["renamed"]:
        print()
        print(f"  !! {len(r['renamed'])} duplicate name(s) RENAMED. Tell these players "
              "-- they type the suffixed name:")
        for row_no, orig, became in r["renamed"]:
            print(f"       row {row_no}: {orig!r}  ->  {became!r}")

    if blockers:
        print()
        print("  " + "-" * 64)
        print("  NEEDS A PERSON:")
        for b in blockers:
            print(f"    * {b}")
        for row_no, n, names in r["oversized"]:
            print(f"      row {row_no}: {n} members -- {', '.join(names)}")
        for row_no, name in r["admin_clashes"]:
            print(f"      row {row_no}: {name!r} collides with DWR_ADMINS")
        for row_no, name in r["no_phone"][:10]:
            print(f"      row {row_no}: {name!r} has no phone")
        print("  " + "-" * 64)
        if not args.force:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            print(f"\n  Wrote {args.out} but SHIPPED NOTHING.")
            print("  Fix the sheet and re-run, or --force to go without them.")
            return 1

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\n  -> {args.out}")

    if args.check:
        print(f"\n  --check: nothing shipped. ({time.time() - started:.0f}s)")
        return 0

    # ------------------------------------------------------------------
    print()
    print("=" * 68)
    print("  2/5  COPY TO VM")
    print("=" * 68)
    rc, out = run(["scp", args.out, f"{args.vm}:{args.remote_dir}/roster_cache.json"])
    if rc != 0:
        raise SystemExit(f"scp failed:\n{out}")
    print(f"  {args.vm}:{args.remote_dir}/roster_cache.json")

    remote = f"cd {args.remote_dir} && "
    print()
    print("=" * 68)
    print("  3/5  COPY INTO THE CONTAINER")
    print("=" * 68)
    # Service name, not a container id: ids change on every recreate.
    rc, out = run(["ssh", args.vm,
                   remote + "docker compose cp roster_cache.json dwr:/app/roster_cache.json"])
    if rc != 0:
        raise SystemExit(f"docker compose cp failed:\n{out}")
    print("  copied to dwr:/app/roster_cache.json")

    print()
    print("=" * 68)
    print("  4/5  RESTART")
    print("=" * 68)
    rc, out = run(["ssh", args.vm, remote + "docker compose --profile dev restart dwr"])
    if rc != 0:
        raise SystemExit(f"restart failed:\n{out}")
    print("  dwr restarted")

    print()
    print("=" * 68)
    print("  5/5  VERIFY WHAT THE SERVER ACTUALLY LOADED")
    print("=" * 68)
    # The point of this step. A restart that "succeeded" proves nothing --
    # the container can come up having refused the roster, or having loaded
    # an older file that was already in the image.
    print("  waiting for warm-up (CLIP loads before the port opens)...")
    seen = ""
    for _ in range(24):          # up to ~2 minutes
        time.sleep(5)
        rc, out = run(["ssh", args.vm, remote + "docker compose logs dwr --tail 60"])
        if "Server ready" in out or "Refusing to start" in out:
            seen = out
            break
    if not seen:
        print("  !! no 'Server ready' line yet. Check by hand:")
        print(f"     ssh {args.vm} '{remote}docker compose logs dwr --tail 60'")
        return 1

    ready = [l for l in seen.splitlines() if "Server ready" in l or "Refusing to start" in l]
    for line in ready[-3:]:
        print(f"  {line.strip()}")

    ok = any(f"{n_players} players" in l and f"{n_teams} teams" in l for l in ready)
    print()
    if ok:
        print(f"  MATCHES the file you just built: {n_players} players, {n_teams} teams.")
    else:
        print(f"  !! the server did NOT report {n_players} players across {n_teams} teams.")
        print("     It may be running an older roster. Do not start the game.")
        return 1

    print(f"\n  Done in {time.time() - started:.0f}s.")
    if r["renamed"]:
        print(f"  REMEMBER: {len(r['renamed'])} renamed player(s) need telling.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
