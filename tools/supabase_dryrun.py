"""Roster dry-run: read the event roster, translate it, and report problems.

Run this against the real Supabase before the event. It fetches the roster,
translates it, prints every defect it finds, and then actually constructs a
GameServer from the result -- so a pass here means the roster will boot the
app, not merely that it parsed.

    # against real Supabase (needs SUPABASE_URL / SUPABASE_KEY)
    python tools/supabase_dryrun.py

    # against built-in mock data, no credentials or network needed
    python tools/supabase_dryrun.py --mock

    # against a JSON file exported from Supabase
    python tools/supabase_dryrun.py --file roster.json

Exit code is 0 if the roster is usable, 1 if not -- so it can gate a deploy.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"))

import paths  # noqa: F401  -- importing this loads .env, so the tool picks up
              # SUPABASE_URL/KEY from the file without needing shell exports
import reader


def load_rows(args):
    if args.mock:
        from mock_supabase import MockSupabase, make_clean_roster
        print("Source: built-in mock roster "
              f"({args.mock_teams} teams x {args.members} members)\n")
        rows = make_clean_roster(teams=args.mock_teams, members_per_team=args.members)
        return reader.fetch_roster_rows(MockSupabase(rows))

    if args.file:
        print(f"Source: {args.file}\n")
        with open(args.file) as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else data.get("data", [])

    print("Source: live Supabase\n")
    client = reader.connect()
    return reader.fetch_roster_rows(client, event_name=args.event_name,
                                    event_slug=args.event_slug)


def redact_rows(rows):
    """Replace attendee names and phone numbers with placeholders.

    Preserves everything that matters for diagnosing a schema mismatch --
    column names, nesting, types, string-vs-list encoding of the JSON column,
    which keys are present or missing per member, and whether ids are null or
    duplicated. Loses only the values that identify real people.
    """
    def scrub_member(member, index):
        if not isinstance(member, dict):
            return member
        scrubbed = dict(member)
        if "name" in scrubbed:
            scrubbed["name"] = f"Person {index}" if scrubbed["name"] else scrubbed["name"]
        if "phone" in scrubbed:
            original = str(scrubbed["phone"] or "")
            # Keep the *format* (country code, separators, length) since that
            # is exactly what broke password derivation.
            scrubbed["phone"] = "".join(
                "9" if character.isdigit() else character for character in original
            )
        for key in ("email", "college", "address"):
            if key in scrubbed and scrubbed[key]:
                scrubbed[key] = f"<redacted {key}>"
        return scrubbed

    counter = [0]

    def scrub_row(row):
        if not isinstance(row, dict):
            return row
        scrubbed = dict(row)
        members = scrubbed.get("team_members")
        was_string = isinstance(members, str)
        if was_string:
            try:
                members = json.loads(members)
            except (json.JSONDecodeError, ValueError):
                return scrubbed
        if isinstance(members, list):
            cleaned = []
            for member in members:
                counter[0] += 1
                cleaned.append(scrub_member(member, counter[0]))
            scrubbed["team_members"] = json.dumps(cleaned) if was_string else cleaned
        return scrubbed

    return [scrub_row(row) for row in rows]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", help="use built-in mock data")
    parser.add_argument("--mock-teams", type=int, default=150)
    parser.add_argument("--file", help="a JSON file of rows exported from Supabase")
    parser.add_argument("--members", type=int, default=4, help="members per team cap")
    parser.add_argument("--event-name", default=reader.DEFAULT_EVENT_NAME)
    parser.add_argument("--event-slug", default=reader.DEFAULT_EVENT_SLUG)
    parser.add_argument("--show", type=int, default=8,
                        help="how many translated credentials to print")
    parser.add_argument("--dump", metavar="PATH",
                        help="write the fetched rows to a JSON file")
    parser.add_argument("--redact", action="store_true",
                        help="with --dump, replace names/phones with placeholders so the "
                             "structure can be shared without exposing attendee PII")
    args = parser.parse_args()

    try:
        rows = load_rows(args)
    except Exception as exc:
        print(f"Could not read the roster: {exc}")
        return 1

    print(f"Rows fetched: {len(rows)}")

    if args.dump:
        payload = redact_rows(rows) if args.redact else rows
        with open(args.dump, "w") as handle:
            json.dump(payload, handle, indent=2, default=str)
        print(f"Wrote {len(rows)} row(s) to {args.dump}"
              f"{' (redacted)' if args.redact else ''}")

    result = reader.translate_roster(rows, max_members_per_team=args.members)

    print()
    print(result.report.summary())

    if result.player_data and args.show:
        print(f"\nFirst {args.show} translated credentials:")
        print(f"  {'id':<8}{'username':<28}{'password':<20}{'team'}")
        for index, (pid, (username, password, team)) in enumerate(result.player_data.items()):
            if index >= args.show:
                break
            print(f"  {str(pid):<8}{username:<28}{password:<20}{team}")

    # The real test: does this roster actually boot the application?
    print()
    if not result.player_data:
        print("No players translated — nothing to build.")
        return 1

    try:
        from models.server import GameServer
    except ImportError as exc:
        # models.server pulls in services.ai_handling, which imports torch and
        # open_clip. Validating a roster shouldn't require the ML stack, so
        # degrade rather than crash.
        print(f"Skipping the GameServer build check: {exc}")
        print("(Install requirements.txt to enable it, or run this on the deploy host.)")
        if result.report.fatal:
            print("\nRoster is NOT usable — fix the FATAL items above.")
            return 1
        print("\nTranslation is clean, but the GameServer build was not verified.")
        return 0

    try:
        server = GameServer(
            server_id=0,
            max_teams=result.team_count,
            max_members_per_team=args.members,
            team_names=[f"Team {i}" for i in range(result.team_count)],
            player_data=result.player_data,
            admin_data={0: ["admin", "change-me"]},
        )
    except ValueError as exc:
        print(f"GameServer REFUSED this roster: {exc}")
        return 1

    print(f"GameServer built successfully: {len(server.players)} players across "
          f"{len(server.teams)} teams.")

    # Spot-check that a translated credential actually authenticates.
    sample_id = next(iter(result.player_data))
    username, password, _team = result.player_data[sample_id]
    if server.players[sample_id].verify_password(password):
        print(f"Credential check: {username!r} authenticates with its derived password.")
    else:
        print("Credential check FAILED — translated password does not verify.")
        return 1

    if result.report.fatal:
        print("\nRoster is NOT usable — fix the FATAL items above.")
        return 1

    print("\nRoster is usable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
